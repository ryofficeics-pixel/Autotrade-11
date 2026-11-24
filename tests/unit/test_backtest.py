import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest

from autotrade.backtest import registry
from autotrade.backtest.config import BacktestConfig
from autotrade.backtest.data import load_ticks
from autotrade.backtest.engine import simulate
from autotrade.backtest.metrics import (
    avg_exposure,
    max_drawdown,
    result_hash,
    summarize,
)
from autotrade.backtest.runner import dev_run, holdout_run, walk_forward
from autotrade.config import AppConfig, RiskLimits
from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.domain.models import (
    CheckStatus,
    CrossCheck,
    MarketSnapshot,
    Pool,
    SafetyEvaluation,
    Verdict,
)
from tests.conftest import MINT, MINT2, T0, WALLET

FX = Decimal(16000)
STEP = 60


def cfg(**risk) -> AppConfig:
    return AppConfig(risk=RiskLimits(usd_idr_rate=FX, **risk))


def ramp(n, flat=20, up=20, pct=Decimal("0.005"), crash_at=None, crash=Decimal("0.5")):
    """Prices: flat, rise, flat; optional crash (price *= crash each tick after crash_at)."""
    out, p = [], Decimal(1)
    for i in range(n):
        if flat <= i < flat + up:
            p *= 1 + pct
        if crash_at is not None and i >= crash_at:
            p *= crash
        out.append(p)
    return out


def seed(db, prices, mint=MINT, pool=WALLET, liq="50000", passing=True, start=T0):
    with db.session() as conn, db.transaction(conn):
        repo.upsert_pool(
            conn,
            Pool(
                address=pool,
                dex="d",
                base_mint=mint,
                quote_mint=MINT2,
                source="dexscreener",
                observed_at=start,
                received_at=start,
            ),
        )
        for i, price in enumerate(prices):
            t = start + timedelta(seconds=STEP * i)
            repo.insert_snapshot(
                conn,
                MarketSnapshot(
                    pool_address=pool,
                    base_mint=mint,
                    quote_mint=MINT2,
                    price_quote="1",
                    price_usd=price,
                    liquidity_usd=liq,
                    buys_5m=30,
                    sells_5m=10,
                    buys_1h=50,
                    sells_1h=40,
                    buys_24h=600,
                    sells_24h=500,
                    source="dexscreener",
                    observed_at=t,
                    received_at=t,
                ),
            )
            if passing:
                repo.insert_evaluation(
                    conn,
                    SafetyEvaluation(
                        mint=mint,
                        ruleset_version="m2-1",
                        evaluated_at=t,
                        verdict=Verdict.PASS,
                        rules=(),
                    ),
                )
                repo.insert_cross_check(
                    conn,
                    CrossCheck(
                        check_type="price_usd", mint=mint, status=CheckStatus.OK, checked_at=t
                    ),
                )


def tend(n, start=T0):
    return start + timedelta(seconds=STEP * n)


def run(db, prices, config=None, bt=None, end=None, **kw):
    seed(db, prices, **kw)
    with db.session() as conn:
        return simulate(
            conn,
            config or cfg(),
            bt or BacktestConfig(failed_tx_rate=Decimal(0)),
            T0,
            end or tend(len(prices)),
        )


def test_uptrend_trade_enters_after_latency_and_exits_on_time_stop(db):
    res = run(db, ramp(130))
    assert len(res.trades) == 1 and res.decisions >= 1
    t = res.trades[0]
    assert t.exit_reason == "time" and not t.censored
    assert t.opened_at > T0 + timedelta(seconds=STEP * 20)  # only after momentum existed
    assert (t.closed_at - t.opened_at).total_seconds() >= 3600
    assert t.entry_fill > t.entry_mid and t.exit_fill < t.exit_mid  # costs against us both ways


def test_entry_executes_strictly_after_decision(db):
    res = run(db, ramp(130), bt=BacktestConfig(failed_tx_rate=Decimal(0), latency_seconds=15))
    t = res.trades[0]
    ticks = load_ticks(db_conn(db), T0, tend(130))
    decision_tick = min(x.t for x in ticks if x.t >= t.opened_at - timedelta(seconds=STEP))
    assert t.opened_at >= decision_tick + timedelta(seconds=15)


def db_conn(db):
    return db.connect()


def test_trade_arithmetic_is_exact(db):
    res = run(db, ramp(130))
    t = res.trades[0]
    rk = cfg().risk
    impact_in = 2 * t.notional / Decimal(50000)
    assert t.entry_fill == t.entry_mid * (1 + impact_in + Decimal("0.005"))
    assert t.qty == t.notional / t.entry_fill
    proceeds_mid = t.qty * t.exit_mid
    impact_out = 2 * proceeds_mid / Decimal(50000)
    assert t.exit_fill == t.exit_mid * (1 - impact_out - Decimal("0.005"))
    proceeds = t.qty * t.exit_fill
    fees = (
        t.notional * rk.dex_fee_fraction
        + rk.network_fee_usd
        + proceeds * rk.dex_fee_fraction
        + rk.network_fee_usd
    )
    assert t.fees == fees
    assert abs(t.net_pnl - (proceeds - t.notional - fees)) < Decimal("1e-20")


def test_stop_exit_gaps_through_the_stop_level(db):
    prices = ramp(60, crash_at=31)
    res = run(db, prices)
    t = res.trades[0]
    assert t.exit_reason == "stop"
    stop = t.entry_mid * Decimal("0.9")
    assert t.exit_mid < stop  # filled at the later observed price, never at the stop
    assert t.net_pnl < 0


def test_dead_token_is_written_down_not_ignored(db):
    prices = ramp(31)  # token stops printing while a position is open
    res = run(db, prices, end=tend(31) + timedelta(hours=3))
    t = res.trades[0]
    assert t.exit_reason == "dead" and t.exit_fill == 0
    assert t.net_pnl < -t.notional * Decimal("0.99")  # total loss of the stake plus entry fees


def test_open_position_at_end_is_censored_and_flagged(db):
    res = run(db, ramp(40))
    t = res.trades[0]
    assert t.censored and t.exit_reason == "open_at_end"
    assert summarize(res)["censored_open_at_end"] == 1


def test_failed_transactions_cost_fees_and_open_nothing(db):
    res = run(db, ramp(130), bt=BacktestConfig(failed_tx_rate=Decimal("0.999999")))
    assert res.trades == [] and res.failed_tx >= 1
    assert res.failed_tx_cost == res.failed_tx * Decimal("0.02")
    assert summarize(res)["fees_usd"] == f"{res.failed_tx_cost:.6f}"


def test_failed_tx_draw_is_deterministic(db):
    bt = BacktestConfig(failed_tx_rate=Decimal("0.5"))
    seed(db, ramp(130))
    with db.session() as conn:
        a = simulate(conn, cfg(), bt, T0, tend(130))
        b = simulate(conn, cfg(), bt, T0, tend(130))
    assert result_hash(summarize(a), a) == result_hash(summarize(b), b)
    other = simulate(
        db.connect(), cfg(), BacktestConfig(failed_tx_rate=Decimal("0.5"), seed="x"), T0, tend(130)
    )
    assert other.ticks == a.ticks


def test_missed_fill_when_no_tick_arrives_in_time(db):
    bt = BacktestConfig(failed_tx_rate=Decimal(0), latency_seconds=200, max_pending_seconds=100)
    res = run(db, ramp(130), bt=bt)
    assert res.trades == [] and res.missed_fills >= 1


def test_no_trades_without_safety_pass_or_price_check(db):
    assert run(db, ramp(130), passing=False).trades == []


def test_no_trade_when_momentum_conditions_fail(db):
    flat = [Decimal(1)] * 130
    assert run(db, flat).trades == [] and run(db, flat).decisions == 0


def test_future_data_cannot_change_past_decisions(db):
    prices = ramp(130)
    seed(db, prices)
    cut = tend(60)
    conn = db.connect()
    before = simulate(conn, cfg(), BacktestConfig(failed_tx_rate=Decimal(0)), T0, cut)
    h_before = result_hash(summarize(before), before)
    with db.session() as c2, db.transaction(c2):  # wild data strictly AFTER the cut
        for i in range(60, 90):
            t = T0 + timedelta(seconds=STEP * i)
            repo.insert_snapshot(
                c2,
                MarketSnapshot(
                    pool_address=WALLET,
                    base_mint=MINT,
                    quote_mint=MINT2,
                    price_quote="1",
                    price_usd="1000",
                    liquidity_usd="1",
                    source="dexscreener",
                    observed_at=t + timedelta(seconds=1),
                    received_at=t,
                ),
            )
    conn2 = db.connect()
    after = simulate(conn2, cfg(), BacktestConfig(failed_tx_rate=Decimal(0)), T0, cut)
    assert result_hash(summarize(after), after) == h_before


def test_requires_fx_rate_and_data(db):
    seed(db, ramp(30))
    with pytest.raises(ValueError, match="USD_IDR"):
        simulate(db.connect(), AppConfig(), BacktestConfig(), T0, tend(30))
    empty = Database(":memory:")
    with empty.session() as conn:
        empty.migrate(conn)
        with pytest.raises(ValueError, match="no stored"):
            dev_run(conn, cfg(), BacktestConfig(), T0)


def test_sizing_parity_with_live_risk_gate(db, clock):
    from autotrade.risk.sizing import compute_sizing

    price, stop, liq = Decimal(2), Decimal("1.8"), Decimal(50000)
    rk = cfg().risk
    sz = compute_sizing(
        price,
        stop,
        liq,
        rk,
        equity_usd=Decimal("62.5"),
        cash_usd=Decimal("62.5"),
        exposure_usd=Decimal(0),
    )
    assert sz is not None and sz.binding == "risk" and Decimal("2.5") < sz.notional < Decimal("2.7")
    assert (
        compute_sizing(
            price,
            Decimal(2),
            liq,
            rk,
            equity_usd=Decimal(1),
            cash_usd=Decimal(1),
            exposure_usd=Decimal(0),
        )
        is None
    )
    assert (
        compute_sizing(
            price,
            stop,
            Decimal(0),
            rk,
            equity_usd=Decimal(1),
            cash_usd=Decimal(1),
            exposure_usd=Decimal(0),
        )
        is None
    )


# ---- metrics -------------------------------------------------------------------------------------
def test_metrics_helpers():
    from datetime import UTC, datetime

    t = lambda s: datetime(2026, 1, 1, 0, 0, s, tzinfo=UTC)  # noqa: E731
    curve = [(t(0), Decimal(100)), (t(1), Decimal(120)), (t(2), Decimal(90)), (t(3), Decimal(110))]
    assert max_drawdown(curve) == Decimal("0.25")
    assert avg_exposure(
        [(t(0), Decimal("0.5")), (t(10), Decimal("0.1")), (t(20), Decimal(0))]
    ) == Decimal("0.3")
    assert avg_exposure([]) == 0


def test_summary_reports_all_required_measures(db):
    res = run(db, ramp(130))
    s = summarize(res)
    for key in (
        "trades",
        "net_pnl_usd",
        "expectancy_return_per_trade",
        "profit_factor",
        "win_rate",
        "max_drawdown",
        "worst_trade_return",
        "p5_trade_return",
        "turnover_x_equity",
        "avg_exposure_fraction",
        "fees_usd",
        "fee_share_of_gross",
        "execution_drag_usd",
        "benchmark_equal_weight_hold_return",
        "sample_warning",
        "missed_fills",
        "failed_transactions",
        "dead_token_exits",
        "exit_reasons",
    ):
        assert key in s
    assert "not statistical evidence" in s["sample_warning"]
    assert Decimal(s["net_pnl_usd"]) == res.trades[0].net_pnl.quantize(Decimal("0.000001"))
    assert s["profit_factor"] is None or Decimal(s["profit_factor"]) >= 0


def test_replay_is_reproducible_and_config_sensitive(db):
    seed(db, ramp(130))
    conn = db.connect()
    bt = BacktestConfig(failed_tx_rate=Decimal(0))
    a = simulate(conn, cfg(), bt, T0, tend(130))
    b = simulate(conn, cfg(), bt, T0, tend(130))
    assert result_hash(summarize(a), a) == result_hash(summarize(b), b)
    c = simulate(
        conn,
        cfg(),
        BacktestConfig(failed_tx_rate=Decimal(0), slippage_buffer=Decimal("0.02")),
        T0,
        tend(130),
    )
    assert result_hash(summarize(c), c) != result_hash(summarize(a), a)
    assert registry.config_hash(cfg(), bt) == registry.config_hash(cfg(), bt)
    assert registry.config_hash(cfg(), bt) != registry.config_hash(
        cfg(min_order_usd=Decimal(2)), bt
    )


# ---- holdout, walk-forward, registry -----------------------------------------------------------
def prepared(db):
    seed(db, ramp(400, flat=20, up=20), mint=MINT)
    return BacktestConfig(failed_tx_rate=Decimal(0))


def test_holdout_lock_is_fixed_once_and_dev_never_reaches_it(db, clock):
    bt = prepared(db)
    with db.session() as conn, db.transaction(conn):
        start, created = registry.lock_holdout(conn, clock.now, Decimal("0.25"))
        again, created2 = registry.lock_holdout(conn, clock.now, Decimal("0.5"))
        assert created and not created2 and again == start
        rep = dev_run(conn, cfg(), bt, clock.now)
    assert rep.window[1] == start
    assert start == T0 + (tend(399) - T0) * 0.75  # last stored tick is index 399
    assert rep.experiment_id and not rep.notes


def test_dev_run_without_lock_warns_in_sample(db, clock):
    bt = prepared(db)
    with db.session() as conn, db.transaction(conn):
        rep = dev_run(conn, cfg(), bt, clock.now)
    assert any("NO HOLDOUT LOCKED" in n for n in rep.notes)


def test_walk_forward_needs_lock_and_reports_folds(db, clock):
    bt = prepared(db)
    with db.session() as conn, db.transaction(conn):
        with pytest.raises(ValueError, match="lock the holdout"):
            walk_forward(conn, cfg(), bt, clock.now, 3)
        registry.lock_holdout(conn, clock.now, Decimal("0.2"))
        with pytest.raises(ValueError, match="at least 2"):
            walk_forward(conn, cfg(), bt, clock.now, 1)
        rep = walk_forward(conn, cfg(), bt, clock.now, 3)
    assert len(rep.folds) == 3 and rep.summary["folds"] == 3
    assert rep.folds[0]["window"][1] == rep.folds[1]["window"][0]  # contiguous, non-overlapping


def test_holdout_contamination_is_recorded(db, clock):
    bt = prepared(db)
    with db.session() as conn, db.transaction(conn):
        registry.lock_holdout(conn, clock.now, Decimal("0.3"))
        first = holdout_run(conn, cfg(), bt, clock.now)
        same = holdout_run(conn, cfg(), bt, clock.now)
        assert not first.contaminated and not same.contaminated
        changed = holdout_run(conn, cfg(min_order_usd=Decimal("1.5")), bt, clock.now)
        assert changed.contaminated and any("CONTAMINATED" in n for n in changed.notes)
        flags = [r["contaminated"] for r in conn.execute("SELECT contaminated FROM experiments")]
        assert flags == [1, 1, 1]  # every holdout run is tainted retroactively


def test_holdout_requires_lock(db, clock):
    bt = prepared(db)
    with db.session() as conn, pytest.raises(ValueError, match="lock the holdout"):
        holdout_run(conn, cfg(), bt, clock.now)


def test_lock_needs_history(db, clock):
    with db.session() as conn, pytest.raises(ValueError, match="not enough"):
        registry.lock_holdout(conn, clock.now, Decimal("0.2"))


def test_hash_is_sha256_hex(db):
    res = run(db, ramp(130))
    h = result_hash(summarize(res), res)
    assert len(h) == 64 and int(h, 16) >= 0 and hashlib.sha256().digest_size == 32
