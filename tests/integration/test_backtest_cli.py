"""M5 through the CLI on a seeded history (synthetic prices, tests only)."""

import sqlite3

from autotrade import cli
from autotrade.data.storage.db import Database
from tests.unit.test_backtest import ramp, seed


def setup(tmp_path, fx="16000"):
    e = {"AUTOTRADE_DB_PATH": str(tmp_path / "b.db"), "AUTOTRADE_USD_IDR_RATE": fx}
    cli.main(["db", "init"], e)
    seed(Database(tmp_path / "b.db"), ramp(400))
    return e


def test_full_backtest_workflow(tmp_path, capsys):
    e = setup(tmp_path)
    assert cli.main(["backtest", "run"], e) == 0
    out = capsys.readouterr().out
    assert "NO HOLDOUT LOCKED" in out and "expectancy_return_per_trade" in out
    assert cli.main(["backtest", "lock", "--fraction", "0.25"], e) == 0
    assert "locked now" in capsys.readouterr().out
    assert cli.main(["backtest", "lock", "--fraction", "0.5"], e) == 0
    assert "already locked; unchanged" in capsys.readouterr().out
    assert cli.main(["backtest", "run"], e) == 0
    run1 = capsys.readouterr().out
    assert "NO HOLDOUT" not in run1 and "result_hash=" in run1
    assert cli.main(["backtest", "run"], e) == 0
    run2 = capsys.readouterr().out
    h = lambda o: o.split("result_hash=")[1].split()[0]  # noqa: E731
    assert h(run1) == h(run2)  # reproducible
    assert cli.main(["backtest", "walkforward", "--folds", "3"], e) == 0
    assert "fold 3:" in capsys.readouterr().out
    assert cli.main(["backtest", "holdout"], e) == 0
    assert "CONTAMINATED" not in capsys.readouterr().out
    assert cli.main(["backtest", "list"], e) == 0
    listing = capsys.readouterr().out
    assert "holdout" in listing and "walkforward" in listing and "dev" in listing


def test_changed_config_on_holdout_is_flagged(tmp_path, capsys):
    e = setup(tmp_path)
    cli.main(["backtest", "lock"], e)
    assert cli.main(["backtest", "holdout"], e) == 0
    capsys.readouterr()
    e2 = {**e, "AUTOTRADE_RISK_MIN_ORDER_USD": "1.5"}
    assert cli.main(["backtest", "holdout"], e2) == 0
    assert "CONTAMINATED" in capsys.readouterr().out
    rows = (
        sqlite3.connect(tmp_path / "b.db")
        .execute("SELECT contaminated FROM experiments WHERE kind='holdout'")
        .fetchall()
    )
    assert all(r[0] == 1 for r in rows)


def test_errors_are_reported_with_exit_1(tmp_path, capsys):
    e = {"AUTOTRADE_DB_PATH": str(tmp_path / "b.db")}
    assert cli.main(["backtest", "run"], e) == 1  # no database
    cli.main(["db", "init"], e)
    assert cli.main(["backtest", "run"], e) == 1
    assert "no stored market snapshots" in capsys.readouterr().err
    seed(Database(tmp_path / "b.db"), ramp(100))
    assert cli.main(["backtest", "run"], e) == 1  # no FX configured
    assert "USD_IDR" in capsys.readouterr().err
    e["AUTOTRADE_USD_IDR_RATE"] = "16000"
    assert cli.main(["backtest", "holdout"], e) == 1
    assert "lock the holdout" in capsys.readouterr().err
    assert cli.main(["backtest", "walkforward"], e) == 1
