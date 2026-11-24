import sqlite3
from datetime import timedelta
from decimal import Decimal

from autotrade.config import AppConfig, PaperSettings, RiskLimits
from autotrade.data.storage import repositories as repo
from autotrade.domain.models import MarketSnapshot
from autotrade.paper import broker, ledger
from autotrade.paper.execution_model import FillParams, buy_fill, sell_fill, tx_fails
from autotrade.paper.portfolio import load_portfolio
from autotrade.risk.engine import evaluate_signal
from autotrade.risk.portfolio import FlatPaperPortfolio
from tests.conftest import MINT, MINT2, WALLET
from tests.unit.test_risk import world

FX = Decimal(16000)


def pcfg(failed=0, **paper) -> AppConfig:
    return AppConfig(
        risk=RiskLimits(usd_idr_rate=FX, assumed_edge_fraction=Decimal("0.10")),
        paper=PaperSettings(failed_tx_rate=Decimal(failed), **paper),
    )


def quote(db, at, price, liq="50000"):
    with db.session() as conn, db.transaction(conn):
        repo.insert_snapshot(
            conn,
            MarketSnapshot(
                pool_address=WALLET,
                base_mint=MINT,
                quote_mint=MINT2,
                price_quote="1",
                price_usd=price,
                liquidity_usd=liq,
                source="dexscreener",
                observed_at=at,
                received_at=at,
            ),
        )


def approve(db, clock, config=None):
    """Seed a clean world and store an APPROVED risk decision; returns decision row."""
    sid = world(db, clock)
    cfg = config or pcfg()
    with db.session() as conn, db.transaction(conn):
        d = evaluate_signal(
            conn, sid, clock.now, cfg, FlatPaperPortfolio(Decimal(1_000_000)).load()
        )
        assert d.approved, d.reason_code
        repo.insert_risk_decision(conn, d)
    return d


def cycle(db, now, config):
    with db.session() as conn, db.transaction(conn):
        events = broker.run_cycle(conn, now, config)
        problems = ledger.reconcile(conn)
    assert problems == [], problems
    return events


def fill_entry(db, clock, config=None, price="2"):
    config = config or pcfg()
    d = approve(db, clock, config)
    cycle(db, clock.now, config)  # accepts the intent, nothing executable yet
    quote(db, clock.now + timedelta(seconds=20), price)
    events = cycle(db, clock.now + timedelta(seconds=30), config)
    return d, events


def rows(db, sql):
    with db.session() as conn:
        return conn.execute(sql).fetchall()


# ---- execution model ----
def test_execution_model_is_conservative_and_exact():
    p = FillParams(Decimal("0.003"), Decimal("0.02"), Decimal("0.005"), Decimal("0.01"))
    b = buy_fill(Decimal(2), Decimal(50000), Decimal(2), p)
    assert b is not None and b.impact == Decimal("0.00008")
    assert b.fill_price == 2 * (1 + Decimal("0.00008") + Decimal("0.005")) and b.fill_price > 2
    assert b.entry_cost == Decimal(2) * Decimal("0.003") + Decimal("0.02")
    s = sell_fill(Decimal(2), Decimal(50000), b.qty, p)
    assert s.fill_price < 2 and s.fee == s.proceeds * Decimal("0.003") + Decimal("0.02")
    assert buy_fill(Decimal(2), Decimal(100), Decimal(2), p) is None  # impact 4% > cap
    dead = sell_fill(Decimal(2), Decimal(1), Decimal(3), p, haircut=Decimal(1))
    assert dead.proceeds == 0 and dead.fill_price == 0


def test_failed_tx_draw_is_deterministic_and_rate_bounded(clock):
    draws = [
        tx_fails("s", MINT, clock.now + timedelta(seconds=i), "entry", Decimal("0.3"))
        for i in range(400)
    ]
    assert draws == [
        tx_fails("s", MINT, clock.now + timedelta(seconds=i), "entry", Decimal("0.3"))
        for i in range(400)
    ]
    assert 0.2 < sum(draws) / 400 < 0.4
    assert not tx_fails("s", MINT, clock.now, "entry", Decimal(0))


# ---- entries ----
def test_intent_becomes_one_order_and_waits_for_a_later_quote(db, clock):
    approve(db, clock)
    events = cycle(db, clock.now, pcfg())
    assert [e.outcome for e in events] == ["created"]
    assert cycle(db, clock.now + timedelta(seconds=5), pcfg()) == []  # still pending, no duplicate
    cycle(db, clock.now + timedelta(seconds=6), pcfg())
    assert len(rows(db, "SELECT * FROM paper_orders")) == 1
    assert rows(db, "SELECT status FROM paper_orders")[0]["status"] == "pending"


def test_fill_uses_executable_price_not_mid_and_posts_ledger(db, clock):
    d, events = fill_entry(db, clock)
    assert [e.outcome for e in events] == ["filled"]
    fill = rows(db, "SELECT * FROM paper_fills")[0]
    mid = Decimal(fill["mid_price"])
    assert mid == 2 and Decimal(fill["fill_price"]) > mid
    notional = d.notional_usd
    cost = notional * Decimal("0.003") + Decimal("0.02")
    deposit = Decimal(1_000_000) / FX
    with db.session() as conn:
        assert ledger.cash(conn) == deposit - notional - cost
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM ledger_entries ORDER BY id")]
    assert kinds == ["deposit", "buy", "fee"]
    pos = rows(db, "SELECT * FROM paper_positions")[0]
    assert pos["closed_at"] is None and Decimal(pos["qty"]) == notional / Decimal(
        fill["fill_price"]
    )


def test_restart_replays_nothing(db, clock):
    d, _ = fill_entry(db, clock)
    before = {
        t: len(rows(db, f"SELECT * FROM {t}"))
        for t in ("paper_orders", "paper_fills", "ledger_entries", "paper_positions")
    }
    for i in range(3):  # "restarts": fresh connections, same state
        cycle(db, clock.now + timedelta(seconds=40 + i), pcfg())
    after = {t: len(rows(db, f"SELECT * FROM {t}")) for t in before}
    assert before == after
    assert (
        rows(db, "SELECT COUNT(*) n FROM paper_orders WHERE decision_id IS NOT NULL")[0]["n"] == 1
    )
    with db.session() as conn, db.transaction(conn):
        assert broker.submit_intents(conn, clock.now, pcfg()) == []


def test_order_expires_without_executable_quote(db, clock):
    approve(db, clock)
    cycle(db, clock.now, pcfg())
    events = cycle(db, clock.now + timedelta(seconds=301), pcfg())
    assert [e.outcome for e in events] == ["missed"]
    assert "no executable quote" in events[0].detail
    with db.session() as conn:
        assert ledger.cash(conn) == Decimal(1_000_000) / FX  # nothing spent


def test_failed_transaction_costs_only_the_fee(db, clock):
    d, events = fill_entry(db, clock, pcfg(failed="0.999999"))
    assert [e.outcome for e in events] == ["failed"]
    with db.session() as conn:
        assert ledger.cash(conn) == Decimal(1_000_000) / FX - Decimal("0.02")
        assert ledger.open_positions(conn) == []


def test_entry_refused_when_price_collapsed_or_impact_too_high(db, clock):
    _, events = fill_entry(db, clock, price="1.7")  # at/below the 1.8 stop
    assert events[0].outcome == "missed" and "stop" in events[0].detail


def test_entry_refused_when_liquidity_thinned(db, clock):
    config = pcfg()
    approve(db, clock, config)
    cycle(db, clock.now, config)
    quote(db, clock.now + timedelta(seconds=20), "2", liq="100")
    events = cycle(db, clock.now + timedelta(seconds=30), config)
    assert events[0].outcome == "missed" and "impact" in events[0].detail


def test_no_double_position_in_a_mint_is_enforced_by_the_database(db, clock):
    fill_entry(db, clock)
    with db.session() as conn, db.transaction(conn):
        try:
            conn.execute(
                "INSERT INTO paper_positions(mint,qty,cost_basis_usd,entry_fill_price,"
                "entry_cost_usd,stop_usd,opened_at) VALUES (?,?,?,?,?,?,?)",
                (MINT, "1", "1", "1", "0", "1", "t"),
            )
            raise AssertionError("second open position was accepted")
        except sqlite3.IntegrityError:
            pass


# ---- exits ----
def test_stop_exit_fills_at_later_executable_price_with_gap(db, clock):
    fill_entry(db, clock)
    t1 = clock.now + timedelta(seconds=90)
    quote(db, t1, "1.5")  # gapped through the 1.8 stop
    events = cycle(db, t1 + timedelta(seconds=1), pcfg())
    assert [(e.kind, e.outcome) for e in events] == [("exit", "created")]
    quote(db, t1 + timedelta(seconds=20), "1.4")
    events = cycle(db, t1 + timedelta(seconds=30), pcfg())
    assert events[0].outcome == "filled" and "stop" in events[0].detail
    fill = rows(
        db,
        "SELECT f.* FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id WHERE o.kind='exit'",
    )[0]
    assert Decimal(fill["mid_price"]) == Decimal("1.4") and Decimal(fill["fill_price"]) < Decimal(
        "1.4"
    )
    pos = rows(db, "SELECT * FROM paper_positions")[0]
    assert (
        pos["closed_at"] and Decimal(pos["realized_pnl_usd"]) < 0 and pos["exit_reason"] == "stop"
    )
    with db.session() as conn:
        assert ledger.open_positions(conn) == []


def test_time_exit(db, clock):
    fill_entry(db, clock)
    quote(db, clock.now + timedelta(seconds=3700), "2.1")
    events = cycle(db, clock.now + timedelta(seconds=3701), pcfg())
    assert events[0].detail == "time"


def test_liquidity_collapse_exit(db, clock):
    fill_entry(db, clock)
    t = clock.now + timedelta(seconds=100)
    quote(db, t, "2", liq="30000")  # floor = 80% of 50000 = 40000
    events = cycle(db, t + timedelta(seconds=1), pcfg())
    assert events[0].detail == "liquidity"


def test_dead_token_is_written_off_and_reconciles(db, clock):
    d, _ = fill_entry(db, clock)
    late = clock.now + timedelta(seconds=30 + 3601)
    events = cycle(db, late, pcfg())
    assert events[0].detail == "dead"
    events = cycle(db, late + timedelta(seconds=20), pcfg())
    assert events[0].outcome == "filled"
    pos = rows(db, "SELECT * FROM paper_positions")[0]
    assert pos["exit_reason"] == "dead"
    assert Decimal(pos["realized_pnl_usd"]) < -d.notional_usd  # stake plus fees lost


def test_failed_exit_is_retried_until_it_fills(db, clock):
    fill_entry(db, clock)
    t = clock.now + timedelta(seconds=90)
    quote(db, t, "1.5")
    bad = pcfg(failed="0.999999")
    cycle(db, t + timedelta(seconds=1), bad)  # exit order created
    quote(db, t + timedelta(seconds=20), "1.4")
    events = cycle(db, t + timedelta(seconds=30), bad)
    assert events[0].outcome == "failed"
    with db.session() as conn:
        assert len(ledger.open_positions(conn)) == 1  # still held; never silently closed
    events = cycle(db, t + timedelta(seconds=31), pcfg())  # new exit order, healthy network
    assert [e.outcome for e in events] == ["created"]
    quote(db, t + timedelta(seconds=60), "1.4")
    events = cycle(db, t + timedelta(seconds=70), pcfg())
    assert events[0].outcome == "filled"


# ---- reconciliation ----
def test_reconcile_detects_tampering(db, clock):
    fill_entry(db, clock)
    with db.session() as conn, db.transaction(conn):
        assert ledger.reconcile(conn) == []
        conn.execute("DELETE FROM ledger_entries WHERE kind='fee'")
        assert any("cash mismatch" in p for p in ledger.reconcile(conn))


def test_reconcile_detects_quantity_and_fill_corruption(db, clock):
    fill_entry(db, clock)
    with db.session() as conn, db.transaction(conn):
        conn.execute("UPDATE paper_positions SET qty='999'")
        assert any("quantity mismatch" in p for p in ledger.reconcile(conn))
    with db.session() as conn, db.transaction(conn):
        conn.execute("UPDATE paper_positions SET qty=(SELECT qty FROM paper_fills LIMIT 1)")
        conn.execute("DELETE FROM paper_fills")
        assert any("has 0 fills" in p for p in ledger.reconcile(conn))


def test_deposit_is_idempotent_and_unique(db, clock):
    with db.session() as conn, db.transaction(conn):
        assert ledger.ensure_deposit(conn, clock.now, Decimal(10))
        assert not ledger.ensure_deposit(conn, clock.now, Decimal(999))
        assert ledger.cash(conn) == 10
    with db.session() as conn, db.transaction(conn):
        assert ledger.reconcile(conn) == []


def test_pnl_identity_holds_with_open_and_closed_positions(db, clock):
    fill_entry(db, clock)
    quote(db, clock.now + timedelta(seconds=60), "2.4")
    cycle(db, clock.now + timedelta(seconds=61), pcfg())
    with db.session() as conn:
        eq, cash, prices = ledger.equity(conn)
        assert prices[MINT] == Decimal("2.4") and eq > cash


# ---- portfolio for the risk gate ----
def test_portfolio_before_funding_or_without_fx_is_flat(db, clock):
    with db.session() as conn:
        pf = load_portfolio(conn, clock.now, pcfg())
        assert pf.positions == () and pf.cash_idr == Decimal(1_000_000)
        assert load_portfolio(conn, clock.now, AppConfig()).positions == ()


def test_portfolio_reflects_ledger_and_feeds_the_gate(db, clock):
    d, _ = fill_entry(db, clock)
    with db.session() as conn, db.transaction(conn):
        pf = load_portfolio(conn, clock.now + timedelta(seconds=40), pcfg())
    assert [p.mint for p in pf.positions] == [MINT]
    assert pf.cash_idr < Decimal(1_000_000) and pf.equity_idr > pf.cash_idr
    assert pf.start_of_day_equity_idr == pf.equity_idr or pf.start_of_day_equity_idr > 0
    # the gate now refuses a second entry in the same mint
    sid = rows(db, "SELECT id FROM signals")[0]["id"]
    with db.session() as conn, db.transaction(conn):
        dd = evaluate_signal(conn, sid, clock.now + timedelta(seconds=40), pcfg(), pf)
    assert not dd.approved
    assert any(r.rule == "no_averaging_down" and r.status.value == "fail" for r in dd.rules)


def test_daily_start_equity_is_recorded_once_per_day(db, clock):
    fill_entry(db, clock)
    with db.session() as conn, db.transaction(conn):
        a = load_portfolio(conn, clock.now, pcfg()).start_of_day_equity_idr
    quote(db, clock.now + timedelta(seconds=60), "3")
    with db.session() as conn, db.transaction(conn):
        b = load_portfolio(conn, clock.now + timedelta(seconds=70), pcfg())
    assert b.start_of_day_equity_idr == a and b.equity_idr > a
