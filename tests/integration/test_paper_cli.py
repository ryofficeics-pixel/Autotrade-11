"""M6 through the CLI: ingest -> signal -> risk intent -> simulated order -> fill -> reconcile."""

import sqlite3
from datetime import timedelta
from decimal import Decimal

from autotrade import cli
from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.domain.models import MarketSnapshot
from autotrade.domain.timeutil import utcnow
from tests.integration.test_discovery_cli import NEW, env, rows, run_cli
from tests.integration.test_risk_cli import RISK_ENV, prepared
from tests.integration.test_signals_cli import POOL, USDC


def snapshot(tmp_path, at, price, liq="17302576.45"):
    db = Database(tmp_path / "d.db")
    with db.session() as conn, db.transaction(conn):
        repo.insert_snapshot(
            conn,
            MarketSnapshot(
                pool_address=POOL,
                base_mint=NEW,
                quote_mint=USDC,
                price_quote="1",
                price_usd=price,
                liquidity_usd=liq,
                source="dexscreener",
                observed_at=at,
                received_at=at,
            ),
        )


def approved_world(tmp_path, monkeypatch, sleeper, **extra):
    prepared(tmp_path)
    e = env(tmp_path, AUTOTRADE_PAPER_FAILED_TX_RATE="0", **RISK_ENV, **extra)
    assert run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10)) == 0
    return e


def test_intent_becomes_simulated_order_then_fill_and_reconciles(
    tmp_path, monkeypatch, capsys, sleeper
):
    e = approved_world(tmp_path, monkeypatch, sleeper)
    out = capsys.readouterr().out
    assert "risk APPROVED" in out and "paper entry #1" in out and "created" in out
    assert rows(tmp_path, "SELECT status FROM paper_orders")[0]["status"] == "pending"
    # a later poll provides the first executable quote
    later = utcnow() + timedelta(seconds=30)
    snapshot(tmp_path, later, "70.5")
    monkeypatch.setattr(cli, "utcnow", lambda: later + timedelta(seconds=5))
    assert cli.main(["paper", "run"], e) == 0
    assert "filled" in capsys.readouterr().out
    assert cli.main(["paper", "reconcile"], e) == 0
    assert "ledger reconciles" in capsys.readouterr().out
    assert cli.main(["paper", "status"], e) == 0
    status = capsys.readouterr().out
    assert "open " in status and "equity_usd=" in status
    fill = rows(tmp_path, "SELECT * FROM paper_fills")[0]
    assert Decimal(fill["fill_price"]) > Decimal(fill["mid_price"]) == Decimal("70.5")
    assert cli.main(["paper", "report"], e) == 0
    assert "reconciliation: ok" in capsys.readouterr().out


def test_paper_run_is_idempotent_across_restarts(tmp_path, monkeypatch, capsys, sleeper):
    e = approved_world(tmp_path, monkeypatch, sleeper)
    later = utcnow() + timedelta(seconds=30)
    snapshot(tmp_path, later, "70.5")
    monkeypatch.setattr(cli, "utcnow", lambda: later + timedelta(seconds=5))
    cli.main(["paper", "run"], e)
    counts = lambda: [  # noqa: E731
        rows(tmp_path, f"SELECT COUNT(*) n FROM {t}")[0]["n"]
        for t in ("paper_orders", "paper_fills", "ledger_entries", "paper_positions")
    ]
    first = counts()
    for _ in range(3):
        cli.main(["paper", "run"], e)
    assert counts() == first == [1, 1, 3, 1]
    capsys.readouterr()


def test_ledger_corruption_engages_kill_switch(tmp_path, monkeypatch, capsys, sleeper):
    e = approved_world(tmp_path, monkeypatch, sleeper)
    later = utcnow() + timedelta(seconds=30)
    snapshot(tmp_path, later, "70.5")
    monkeypatch.setattr(cli, "utcnow", lambda: later + timedelta(seconds=5))
    cli.main(["paper", "run"], e)
    sqlite3.connect(tmp_path / "d.db").execute(
        "DELETE FROM ledger_entries WHERE kind='fee'"
    ).connection.commit()
    capsys.readouterr()
    assert cli.main(["paper", "reconcile"], e) == 1
    assert "cash mismatch" in capsys.readouterr().out
    cli.main(["paper", "run"], e)
    err = capsys.readouterr().err
    assert "LEDGER MISMATCH" in err
    assert cli.main(["risk", "kill-switch", "status"], e) == 0
    assert "ledger mismatch" in capsys.readouterr().out


def test_open_position_mint_stays_polled_and_blocks_reentry(tmp_path, monkeypatch, capsys, sleeper):
    e = approved_world(tmp_path, monkeypatch, sleeper)
    later = utcnow() + timedelta(seconds=30)
    snapshot(tmp_path, later, "70.5")
    monkeypatch.setattr(cli, "utcnow", lambda: later + timedelta(seconds=5))
    cli.main(["paper", "run"], e)
    db = Database(tmp_path / "d.db")
    with db.session() as conn:
        from autotrade.config import AppConfig

        assert NEW in cli._all_mints(
            AppConfig.from_env({**e, "AUTOTRADE_DISCOVERY_ENABLED": "false"}), conn
        )


def test_paper_commands_without_funding_or_db(tmp_path, capsys):
    e = env(tmp_path, **RISK_ENV)
    assert cli.main(["paper", "status"], e) == 1
    cli.main(["db", "init"], e)
    assert cli.main(["paper", "status"], e) == 3
    assert "not funded" in capsys.readouterr().out
    assert cli.main(["paper", "run"], e) == 0  # funds the ledger, nothing else to do
    assert cli.main(["paper", "report"], e) == 0
    assert "reconciliation: ok" in capsys.readouterr().out
