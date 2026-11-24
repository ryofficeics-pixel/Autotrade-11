import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from autotrade.config import AppConfig, RiskLimits
from autotrade.data.storage import repositories as repo
from autotrade.domain.models import (
    CheckStatus,
    CrossCheck,
    HealthStatus,
    MarketSnapshot,
    Pool,
    ProviderHealth,
    RiskDecision,
    RuleResult,
    RuleStatus,
    SafetyEvaluation,
    Signal,
    Verdict,
)
from autotrade.risk import state
from autotrade.risk.engine import decide_safely, evaluate_signal, idempotency_key
from autotrade.risk.portfolio import FlatPaperPortfolio, PortfolioState, Position
from tests.conftest import MINT, MINT2, WALLET, WALLET2

FX = Decimal(16000)
EQUITY_IDR = Decimal(1_000_000)
E = EQUITY_IDR / FX  # 62.5 USD
PRICE, STOP, LIQ = Decimal("2"), Decimal("1.8"), Decimal("50000")  # d = 10%


def cfg(**risk) -> AppConfig:
    base = dict(usd_idr_rate=FX, assumed_edge_fraction=Decimal("0.10"))
    return AppConfig(risk=RiskLimits(**{**base, **risk}))


def world(db, clock, **o):
    """Seed everything an approval needs; overrides break one thing at a time."""
    now = clock.now
    quote_at = now - timedelta(seconds=o.get("quote_age", 1))
    with db.session() as conn, db.transaction(conn):
        eid = repo_eval(conn, now, o.get("verdict", Verdict.PASS), o.get("safety_age", 10))
        sig = Signal(
            mint=MINT,
            strategy_version="hybrid-v1",
            created_at=now - timedelta(seconds=5),
            expires_at=now + timedelta(seconds=o.get("expires_in", 300)),
            score=5,
            reasons=("MOMENTUM_15M",),
            invalidation={"price_usd_below": str(o.get("stop", STOP))},
            feature_as_of=now - timedelta(seconds=5),
            safety_evaluation_id=eid,
        )
        repo.insert_signal(conn, sig)
        repo.upsert_pool(
            conn,
            Pool(
                address=WALLET,
                dex="d",
                base_mint=MINT,
                quote_mint=MINT2,
                source="dexscreener",
                observed_at=quote_at,
                received_at=quote_at,
            ),
        )
        if o.get("quote", True):
            repo.insert_snapshot(
                conn,
                MarketSnapshot(
                    pool_address=WALLET,
                    base_mint=MINT,
                    quote_mint=MINT2,
                    price_quote="1",
                    price_usd=o.get("price", PRICE),
                    liquidity_usd=o.get("liq", LIQ),
                    source="dexscreener",
                    observed_at=quote_at,
                    received_at=quote_at,
                ),
            )
        for name in ("solana_rpc", "dexscreener", "jupiter"):
            status = o.get("health", {}).get(name, HealthStatus.HEALTHY)
            if status is not None:
                repo.record_health(
                    conn,
                    ProviderHealth(
                        provider=name,
                        status=status,
                        checked_at=now - timedelta(seconds=o.get("health_age", 5)),
                    ),
                )
        check = o.get("price_check", CheckStatus.OK)
        if check is not None:
            repo.insert_cross_check(
                conn,
                CrossCheck(
                    check_type="price_usd",
                    mint=MINT,
                    status=check,
                    checked_at=now - timedelta(seconds=5),
                    deviation=Decimal("0.01"),
                    detail="x",
                ),
            )
        return conn.execute("SELECT id FROM signals").fetchone()["id"]


def repo_eval(conn, now, verdict, age):
    ev = SafetyEvaluation(
        mint=MINT,
        ruleset_version="m2-1",
        evaluated_at=now - timedelta(seconds=age),
        verdict=verdict,
        rules=(),
    )
    repo.insert_evaluation(conn, ev)
    return conn.execute("SELECT MAX(id) FROM safety_evaluations").fetchone()[0]


def flat(equity=EQUITY_IDR):
    return FlatPaperPortfolio(equity).load()


def decide(db, clock, sid, config=None, pf=None):
    with db.session() as conn, db.transaction(conn):
        return evaluate_signal(conn, sid, clock.now, config or cfg(), pf or flat())


def rule(d: RiskDecision, name: str) -> RuleResult:
    return next(r for r in d.rules if r.rule == name)


# ---- approval and sizing ---------------------------------------------------------------------
def test_approves_clean_world_with_exact_risk_bounded_size(db, clock):
    sid = world(db, clock)
    d = decide(db, clock, sid)
    assert d.approved and d.reason_code == "APPROVED"
    assert all(r.status is RuleStatus.PASS for r in d.rules)
    n, f, fixed = d.notional_usd, Decimal("0.003"), Decimal("0.02")
    dd = (PRICE - STOP) / PRICE
    used = n * dd + 2 * f * n + 4 * n * n / LIQ + 2 * fixed  # loss at stop + round-trip costs
    risk_budget = E * Decimal("0.005")
    assert used <= risk_budget and risk_budget - used < Decimal("0.0001")  # tight, never over
    assert d.quantity == n / PRICE and d.stop_price_usd == STOP and d.entry_price_usd == PRICE
    assert d.notional_idr == n * FX
    assert "risk" in rule(d, "order_size").detail
    assert d.inputs["signal_id"] == sid and d.inputs["snapshot_id"]


def test_position_cap_binds_when_risk_budget_is_large(db, clock):
    sid = world(db, clock)
    d = decide(
        db,
        clock,
        sid,
        cfg(
            risk_per_trade_fraction=Decimal("0.5"),
            max_position_fraction=Decimal("0.02"),
            max_total_exposure_fraction=Decimal("0.30"),
        ),
    )
    assert d.approved and d.notional_usd == (E * Decimal("0.02")).quantize(
        Decimal("0.000001"), rounding="ROUND_DOWN"
    )
    assert "position_cap" in rule(d, "order_size").detail


def test_cash_reserve_and_exposure_cap_bind(db, clock):
    sid = world(db, clock)
    big = dict(risk_per_trade_fraction=Decimal("0.5"))
    # cash 720k IDR = 45 USD; reserve = 70% of 62.5 = 43.75 => exactly 1.25 USD may be spent
    pf = PortfolioState(EQUITY_IDR, Decimal(720_000), EQUITY_IDR)
    d = decide(db, clock, sid, cfg(**big), pf)
    assert d.approved and "cash_after_reserve" in rule(d, "order_size").detail
    assert d.notional_usd == Decimal("1.25")
    # 250k IDR already deployed = 15.625 USD; 30% cap = 18.75 => 3.125 USD of room
    pf2 = PortfolioState(
        EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, positions=(Position(WALLET2, Decimal(250_000)),)
    )
    d2 = decide(db, clock, sid, cfg(**big), pf2)
    assert d2.approved and "exposure_room" in rule(d2, "order_size").detail
    assert d2.notional_usd == Decimal("3.125")


def test_impact_cap_binds_on_thin_liquidity(db, clock):
    sid = world(db, clock, liq=Decimal("100"))
    d = decide(
        db,
        clock,
        sid,
        cfg(
            risk_per_trade_fraction=Decimal("0.5"),
            min_order_usd=Decimal("0.1"),
            max_fixed_cost_fraction=Decimal("0.99"),
            assumed_edge_fraction=Decimal("0.9"),
        ),
    )
    assert d.approved and d.notional_usd == Decimal("0.5")  # 1% impact * 100 USD liquidity / 2
    assert "impact_cap" in rule(d, "order_size").detail
    assert 2 * d.notional_usd / Decimal(100) <= Decimal("0.01")


def test_uneconomic_small_orders_rejected(db, clock):
    sid = world(db, clock)
    d = decide(db, clock, sid, cfg(min_order_usd=Decimal("10")))
    assert not d.approved and rule(d, "order_size").status is RuleStatus.FAIL
    d = decide(db, clock, sid, cfg(network_fee_usd=Decimal("0.5")))  # fixed costs eat the budget
    assert not d.approved and d.reason_code in {"ORDER_SIZE", "FIXED_COSTS"}


# ---- every gate rejects ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"expires_in": 0}, "SIGNAL_UNEXPIRED"),
        ({"verdict": Verdict.REJECT}, "SAFETY_PASS"),
        ({"safety_age": 301}, "SAFETY_PASS"),
        ({"health": {"jupiter": HealthStatus.DOWN}}, "PROVIDERS_HEALTHY"),
        ({"health": {"dexscreener": HealthStatus.DEGRADED}}, "PROVIDERS_HEALTHY"),
        ({"health": {"solana_rpc": None}}, "PROVIDERS_HEALTHY"),
        ({"health_age": 121}, "PROVIDERS_HEALTHY"),
        ({"quote_age": 6}, "QUOTE_FRESH"),
        ({"quote": False}, "QUOTE_FRESH"),
        ({"price_check": CheckStatus.DISCREPANCY}, "QUOTE_CONSISTENT"),
        ({"price_check": CheckStatus.UNAVAILABLE}, "QUOTE_CONSISTENT"),
        ({"price_check": None}, "QUOTE_CONSISTENT"),
        ({"stop": Decimal("2")}, "STOP_DISTANCE"),
        ({"stop": Decimal("2.5")}, "STOP_DISTANCE"),
    ],
)
def test_gate_rejects(db, clock, overrides, code):
    sid = world(db, clock, **overrides)
    d = decide(db, clock, sid)
    assert not d.approved and d.reason_code == code
    assert d.notional_usd is None and d.quantity is None
    assert rule(d, code.lower()).status is not RuleStatus.PASS


def test_quote_age_boundary(db, clock):
    assert decide(db, clock, world(db, clock, quote_age=5)).approved  # exactly at the 5s limit


def test_unknown_inputs_are_unknown_not_pass(db, clock):
    sid = world(db, clock, quote=False)
    d = decide(db, clock, sid)
    assert rule(d, "quote_fresh").status is RuleStatus.UNKNOWN
    d = decide(db, clock, sid, AppConfig())  # no FX configured at all
    assert rule(d, "fx_rate").status is RuleStatus.UNKNOWN and not d.approved


def test_fx_rate_is_never_defaulted(db, clock):
    sid = world(db, clock)
    d = decide(db, clock, sid, AppConfig(risk=RiskLimits(assumed_edge_fraction=Decimal("0.1"))))
    assert not d.approved and d.reason_code == "FX_RATE"


def test_edge_must_be_stated_and_exceed_costs(db, clock):
    sid = world(db, clock)
    unset = AppConfig(risk=RiskLimits(usd_idr_rate=FX))
    d = decide(db, clock, sid, unset)
    assert not d.approved and d.reason_code == "EDGE_VS_COSTS"
    assert rule(d, "edge_vs_costs").status is RuleStatus.UNKNOWN
    d = decide(
        db, clock, sid, cfg(assumed_edge_fraction=Decimal("0.02"))
    )  # costs ~2.2% + 1% margin
    assert not d.approved and rule(d, "edge_vs_costs").status is RuleStatus.FAIL


def test_mode_must_be_paper():
    with pytest.raises(ValidationError):
        AppConfig(mode="live")


# ---- portfolio rules and boundaries ------------------------------------------------------------
def test_daily_loss_stop_boundary(db, clock):
    sid = world(db, clock)
    stop = Decimal("0.02") * EQUITY_IDR  # 20,000
    at = PortfolioState(EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, realized_pnl_today_idr=-stop)
    just = PortfolioState(EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, realized_pnl_today_idr=-stop + 1)
    assert decide(db, clock, sid, pf=at).reason_code == "DAILY_LOSS_STOP"
    assert decide(db, clock, sid, pf=just).approved


def test_concurrent_position_boundary(db, clock):
    sid = world(db, clock)
    held = lambda n: tuple(Position(f"m{i}", Decimal(1000)) for i in range(n))  # noqa: E731
    three = PortfolioState(EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, positions=held(3))
    two = PortfolioState(EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, positions=held(2))
    assert decide(db, clock, sid, pf=three).reason_code == "MAX_POSITIONS"
    assert decide(db, clock, sid, pf=two).approved


def test_no_averaging_down(db, clock):
    sid = world(db, clock)
    pf = PortfolioState(
        EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, positions=(Position(MINT, Decimal(1000)),)
    )
    assert decide(db, clock, sid, pf=pf).reason_code == "NO_AVERAGING_DOWN"


def test_cash_below_reserve_and_no_exposure_room_reject(db, clock):
    sid = world(db, clock)
    low = PortfolioState(EQUITY_IDR, Decimal(700_000), EQUITY_IDR)  # exactly the 70% reserve
    assert decide(db, clock, sid, pf=low).reason_code == "CASH_RESERVE"
    full = PortfolioState(
        EQUITY_IDR, EQUITY_IDR, EQUITY_IDR, positions=(Position(WALLET2, Decimal(300_000)),)
    )
    assert decide(db, clock, sid, pf=full).reason_code == "EXPOSURE_ROOM"


def test_duplicate_intent_and_cooldown(db, clock):
    sid = world(db, clock)
    with db.session() as conn, db.transaction(conn):
        first = decide_safely(conn, sid, clock.now, cfg(), flat())
        assert repo.insert_risk_decision(conn, first) is repo.Outcome.INSERTED
        second = decide_safely(conn, sid, clock.now, cfg(), flat())
    assert first.approved and not second.approved
    assert (
        second.reason_code == "DUPLICATE_INTENT"
        and rule(second, "cooldown").status is RuleStatus.FAIL
    )
    assert first.idempotency_key == second.idempotency_key


def test_cooldown_expires(db, clock):
    sid = world(db, clock)
    with db.session() as conn, db.transaction(conn):
        repo.insert_risk_decision(conn, decide_safely(conn, sid, clock.now, cfg(), flat()))
    # same mint, new signal after the cooldown but a fresh feature time => allowed again
    later = clock.now + timedelta(seconds=3601)
    with db.session() as conn:
        d = evaluate_signal(conn, sid, later, cfg(cooldown_seconds=3600), flat())
    assert rule(d, "cooldown").status is RuleStatus.PASS


def test_db_blocks_a_second_approval_for_one_key(db, clock):
    sid = world(db, clock)
    with db.session() as conn, db.transaction(conn):
        d = evaluate_signal(conn, sid, clock.now, cfg(), flat())
        assert repo.insert_risk_decision(conn, d) is repo.Outcome.INSERTED
        assert repo.insert_risk_decision(conn, d) is repo.Outcome.DUPLICATE
        assert conn.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == 1
        rejected = d.model_copy(
            update={
                "approved": False,
                "notional_usd": None,
                "quantity": None,
                "stop_price_usd": None,
                "notional_idr": None,
                "entry_price_usd": None,
                "reason_code": "X",
                "rules": (RuleResult(rule="x", status=RuleStatus.FAIL),),
            }
        )
        assert repo.insert_risk_decision(conn, rejected) is repo.Outcome.INSERTED


# ---- kill switch and circuit breaker -----------------------------------------------------------
def test_kill_switch_blocks_and_persists_across_connections(db, clock):
    sid = world(db, clock)
    with db.session() as conn, db.transaction(conn):
        state.engage(conn, clock.now, "operator")
    with db.session() as conn:  # a brand-new connection = a restart
        assert state.status(conn) == (True, "operator")
    d = decide(db, clock, sid)
    assert not d.approved and d.reason_code == "KILL_SWITCH"
    with db.session() as conn, db.transaction(conn):
        state.release(conn, clock.now)
    assert decide(db, clock, sid).approved


def test_unknown_kill_switch_value_halts(db, clock):
    sid = world(db, clock)
    with db.session() as conn:
        conn.execute("INSERT INTO risk_state VALUES ('halt','???',NULL,'t')")
    assert decide(db, clock, sid).reason_code == "KILL_SWITCH"


def test_engine_exception_halts_new_entries(db, clock):
    sid = world(db, clock)
    with db.session() as conn:
        conn.execute("UPDATE signals SET invalidation='{not json'")
    with db.session() as conn, db.transaction(conn):
        d = decide_safely(conn, sid, clock.now, cfg(), flat())
    assert not d.approved and d.reason_code == "ENGINE_ERROR"
    with db.session() as conn:
        halted, why = state.status(conn)
    assert halted and "JSONDecodeError" in why
    with db.session() as conn:
        conn.execute('UPDATE signals SET invalidation=\'{"price_usd_below": "1.8"}\'')
    assert (
        decide(db, clock, sid).reason_code == "KILL_SWITCH"
    )  # stays halted until a human releases


def test_missing_signal_is_a_caller_error_not_a_halt(db, clock):
    with db.session() as conn, pytest.raises(LookupError):
        decide_safely(conn, 999, clock.now, cfg(), flat())
    with db.session() as conn:
        assert state.status(conn) == (False, None)


# ---- determinism, model invariants, boundaries ---------------------------------------------------
def test_decisions_are_deterministic(db, clock):
    sid = world(db, clock)
    a, b = decide(db, clock, sid), decide(db, clock, sid)
    assert a == b


def test_idempotency_key_depends_on_signal_identity(db, clock):
    sid = world(db, clock)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
    assert idempotency_key(row) == idempotency_key(row) and len(idempotency_key(row)) == 32


def test_risk_decision_model_invariants():
    ok = RuleResult(rule="r", status=RuleStatus.PASS)
    bad = RuleResult(rule="r", status=RuleStatus.FAIL)
    common = dict(
        signal_id=1,
        mint=MINT,
        decided_at="2026-01-01T00:00:00Z",
        idempotency_key="k",
        reason_code="X",
    )
    with pytest.raises(ValidationError):  # approved without size
        RiskDecision(approved=True, rules=(ok,), **common)
    with pytest.raises(ValidationError):  # rejected carrying a size
        RiskDecision(
            approved=False,
            rules=(bad,),
            notional_usd="1",
            quantity="1",
            stop_price_usd="1",
            **common,
        )
    with pytest.raises(ValidationError):  # approved with a failing rule
        RiskDecision(
            approved=True,
            rules=(bad,),
            notional_usd="1",
            quantity="1",
            stop_price_usd="1",
            **common,
        )


def test_schema_rejects_approved_row_without_size(db):
    with db.session() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO risk_decisions(signal_id,mint,decided_at,approved,idempotency_key,"
            "reason_code,rules,inputs) VALUES (1,'m','t',1,'k','APPROVED','[]','{}')"
        )


def test_risk_package_cannot_reach_network_or_signing():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "autotrade" / "risk"
    imported = set()
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not [
        m
        for m in imported
        if any(w in m for w in ("httpx", "providers", "broker", "signer", "execution"))
    ]


def test_risk_env_config_and_validation():
    c = AppConfig.from_env(
        {"AUTOTRADE_USD_IDR_RATE": "16250.5", "AUTOTRADE_RISK_ASSUMED_EDGE_FRACTION": "0.08"}
    )
    assert c.risk.usd_idr_rate == Decimal("16250.5") and c.risk.assumed_edge_fraction == Decimal(
        "0.08"
    )
    assert AppConfig().risk.usd_idr_rate is None and AppConfig().risk.assumed_edge_fraction is None
    for bad in ({"AUTOTRADE_USD_IDR_RATE": "0"}, {"AUTOTRADE_RISK_ASSUMED_EDGE_FRACTION": "1.5"}):
        with pytest.raises(ValueError):
            AppConfig.from_env(bad)
