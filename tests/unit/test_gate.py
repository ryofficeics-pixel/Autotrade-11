import json
from datetime import timedelta
from decimal import Decimal

import pytest

from autotrade import cli
from autotrade.config import AppConfig, GateSettings
from autotrade.ops.alerts import Alert, emit
from autotrade.ops.gate import (
    Gate,
    consecutive_days,
    evaluate,
    lower_bound,
    realized_max_drawdown,
)
from autotrade.paper import ledger
from tests.conftest import T0


def by_name(items):
    return {i.name: i for i in items}


def run_on(db, day_offsets, status="success"):
    with db.session() as conn:
        for off in day_offsets:
            t = (T0 + timedelta(days=off)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "INSERT INTO ingestion_runs(job,provider,status,started_at,finished_at) "
                "VALUES ('j','p',?,?,?)",
                (status, t, t),
            )


def position(db, n, pnl, cost="10", day=0):
    t = (T0 + timedelta(days=day, minutes=n)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO paper_positions(mint,qty,cost_basis_usd,entry_fill_price,entry_cost_usd,"
            "stop_usd,opened_at,closed_at,exit_reason,realized_pnl_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"m{n}", "1", cost, "1", "0", "1", t, t, "time", str(pnl)),
        )


def cfg(**gate):
    return AppConfig(gate=GateSettings(**gate))


def test_consecutive_days_counts_trailing_run(db, clock):
    run_on(db, [0, 1, 2, 4, 5])
    with db.session() as conn:
        assert consecutive_days(conn, (T0 + timedelta(days=5)).date()) == 2  # gap before day 4
        assert consecutive_days(conn, (T0 + timedelta(days=6)).date()) == 2  # today incomplete
        assert consecutive_days(conn, (T0 + timedelta(days=8)).date()) == 0  # outage > 1 day
        assert consecutive_days(conn, (T0 + timedelta(days=2)).date()) == 3


def test_failed_runs_do_not_count_as_operating_days(db):
    run_on(db, [0, 1], status="failed")
    with db.session() as conn:
        assert consecutive_days(conn, T0.date()) == 0


def test_statistics_helpers():
    assert lower_bound([Decimal(1)], Decimal("1.645")) is None
    mean, low = lower_bound([Decimal("0.1")] * 3 + [Decimal("0.3")], Decimal("1.645"))
    assert mean == Decimal("0.15") and low < mean
    pos = lower_bound([Decimal("0.05") + Decimal(i % 3) / 100 for i in range(60)], Decimal("1.645"))
    assert pos is not None and pos[1] > 0


def test_drawdown_uses_realized_equity_only(db, clock):
    with db.session() as conn:
        ledger.ensure_deposit(conn, clock.now, Decimal(100))
    position(db, 1, "10")  # 110
    position(db, 2, "-22")  # 88 => 20% below the 110 peak
    position(db, 3, "5")
    with db.session() as conn:
        assert realized_max_drawdown(conn) == Decimal("0.2")


def test_empty_system_is_not_ready_and_never_unlocks_live(db, clock):
    with db.session() as conn:
        items = by_name(evaluate(conn, clock.now, AppConfig()))
    assert items["30 consecutive paper days"].status is Gate.NOT_MET
    assert items["explicit user authorisation of live mode"].status is Gate.NOT_MET
    assert items["signer and transaction integration independently reviewed"].status is Gate.MANUAL
    assert items["fee and loss budget explicitly approved"].status is Gate.UNKNOWN
    assert items["positive expectancy on the untouched holdout"].status is Gate.UNKNOWN
    assert items["drawdown within approved budget"].status is Gate.UNKNOWN


def test_all_evidence_items_can_be_met_but_review_and_authorisation_stay_open(
    db, clock, monkeypatch
):
    # The hand-made positions below have no matching fills/ledger rows, so the real reconciliation
    # (correctly) objects; this test isolates the statistics, so health is stubbed clean.
    monkeypatch.setattr("autotrade.ops.gate.collect_issues", lambda *a, **k: [])
    config = cfg(
        min_days=3,
        min_trades=20,
        min_holdout_trades=5,
        max_drawdown_fraction=Decimal("0.5"),
        approved_loss_budget_usd=Decimal(20),
    )
    run_on(db, [0, 1, 2])
    with db.session() as conn:
        ledger.ensure_deposit(conn, clock.now, Decimal(1000))
        conn.execute(
            "INSERT INTO experiments(created_at,kind,config_hash,window_from,window_to,"
            "summary,result_hash,contaminated) VALUES ('t','holdout','h','a','b',?,'r',0)",
            (json.dumps({"trades": 8, "expectancy_return_per_trade": "0.02"}),),
        )
    for i in range(30):
        position(db, i, "1.0" if i % 3 else "0.5")  # steady small winners
    now = T0 + timedelta(days=2, seconds=60)  # right after the last successful run
    with db.session() as conn:
        items = evaluate(conn, now, config)
    evidence = [i for i in items if i.evidence_item]
    assert all(i.status is Gate.MET for i in evidence), [
        (i.name, i.status.value, i.evidence) for i in evidence
    ]
    assert [i.status for i in items if not i.evidence_item] == [Gate.MANUAL, Gate.NOT_MET]


def test_negative_or_noisy_expectancy_does_not_pass(db, clock):
    config = cfg(min_trades=20)
    with db.session() as conn:
        ledger.ensure_deposit(conn, clock.now, Decimal(1000))
    for i in range(24):  # mean slightly positive but far inside the noise
        position(db, i, "5" if i % 2 else "-4.5")
    with db.session() as conn:
        item = by_name(evaluate(conn, clock.now, config))[
            "positive net expectancy (paper, after costs)"
        ]
    assert item.status is Gate.NOT_MET and "lower bound" in item.evidence


def test_contaminated_holdout_fails_the_item(db, clock):
    with db.session() as conn:
        conn.execute(
            "INSERT INTO experiments(created_at,kind,config_hash,window_from,window_to,"
            "summary,result_hash,contaminated) VALUES ('t','holdout','h','a','b',?,'r',1)",
            (json.dumps({"trades": 99, "expectancy_return_per_trade": "0.5"}),),
        )
        item = by_name(evaluate(conn, clock.now, AppConfig()))[
            "positive expectancy on the untouched holdout"
        ]
    assert item.status is Gate.NOT_MET and "CONTAMINATED" in item.evidence


def test_ledger_mismatch_alert_blocks(db, clock, tmp_path):
    from autotrade.config import OpsSettings

    with db.session() as conn:
        ledger.ensure_deposit(conn, clock.now, Decimal(10))
        emit(
            conn,
            clock.now,
            OpsSettings(alert_file=tmp_path / "a.log"),
            Alert("ledger_mismatch", "critical", "cash mismatch"),
        )
        item = by_name(evaluate(conn, clock.now, AppConfig()))["zero ledger mismatches"]
    assert item.status is Gate.NOT_MET


def test_gate_cli_reports_and_cannot_enable_live(tmp_path, capsys):
    e = {"AUTOTRADE_DB_PATH": str(tmp_path / "g.db")}
    assert cli.main(["gate"], e) == 1  # no database
    cli.main(["db", "init"], e)
    capsys.readouterr()
    assert cli.main(["gate"], e) == 1
    out = capsys.readouterr().out
    assert "NOT MET" in out and "LIVE TRADING REMAINS LOCKED" in out
    assert "[MANUAL  ] signer and transaction integration independently reviewed" in out


def test_gate_settings_validation_and_env():
    c = AppConfig.from_env(
        {"AUTOTRADE_GATE_MIN_TRADES": "250", "AUTOTRADE_GATE_MAX_DRAWDOWN": "0.15"}
    )
    assert c.gate.min_trades == 250 and c.gate.max_drawdown_fraction == Decimal("0.15")
    assert (
        AppConfig().gate.max_drawdown_fraction is None
        and AppConfig().gate.approved_loss_budget_usd is None
    )
    for bad in ({"AUTOTRADE_GATE_MAX_DRAWDOWN": "1"}, {"AUTOTRADE_GATE_MIN_DAYS": "0"}):
        with pytest.raises(ValueError):
            AppConfig.from_env(bad)


def test_unreconciled_ledger_blocks_the_gate_even_with_great_statistics(db, clock):
    run_on(db, [0])
    with db.session() as conn:
        ledger.ensure_deposit(conn, clock.now, Decimal(1000))
    for i in range(30):
        position(db, i, "1.0")  # positions with no fills/ledger rows behind them
    with db.session() as conn:
        items = by_name(evaluate(conn, T0 + timedelta(seconds=60), cfg(min_trades=20)))
    item = items["no unresolved critical operational/reconciliation issue"]
    assert item.status is Gate.NOT_MET and "ledger_mismatch" in item.evidence
