import subprocess
import sys

import pytest

from autotrade import cli
from tests.conftest import MINT, WALLET, make_rpc, mint_account, rpc_transport, sig


def env(tmp_path, **extra):
    return {"AUTOTRADE_DB_PATH": str(tmp_path / "cli.db"), **extra}


def test_status_without_db(tmp_path, capsys):
    assert cli.main(["status"], env(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "mode=paper" in out and "live_enabled=False" in out and "not initialized" in out


def test_doctor_reports_paper_and_locked(tmp_path, capsys):
    assert cli.main(["doctor"], env(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "execution mode=PAPER; live execution=LOCKED" in out and "not initialized" in out


def test_db_init_check_status_flow(tmp_path, capsys):
    e = env(tmp_path)
    assert cli.main(["db", "check"], e) == 1  # no database yet
    assert cli.main(["db", "init"], e) == 0
    assert cli.main(["db", "init"], e) == 0  # idempotent
    assert "already current" in capsys.readouterr().out
    assert cli.main(["db", "check"], e) == 0
    assert cli.main(["status"], e) == 0
    out = capsys.readouterr().out
    assert "schema v1/1" in out and "freshness market_snapshots: unknown" in out


def test_db_check_detects_corruption(tmp_path, capsys):
    e = env(tmp_path)
    cli.main(["db", "init"], e)
    import sqlite3

    c = sqlite3.connect(tmp_path / "cli.db")
    c.execute("UPDATE schema_migrations SET checksum='x'")
    c.commit()
    c.close()
    capsys.readouterr()
    assert cli.main(["db", "check"], e) == 1
    assert "PROBLEMS" in capsys.readouterr().out


def test_bad_config_is_exit_2(tmp_path, capsys):
    assert cli.main(["status"], env(tmp_path, AUTOTRADE_LIVE_ENABLED="true")) == 2
    assert cli.main(["status"], env(tmp_path, AUTOTRADE_MODE="live")) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_usage_errors_exit_2(tmp_path):
    assert cli.main(["nonsense"], env(tmp_path)) == 2
    assert cli.main(["ingest"], env(tmp_path)) == 2
    assert cli.main([], env(tmp_path)) == 2


def test_offline_mode_skips_network(tmp_path, capsys):
    e = env(tmp_path, AUTOTRADE_OFFLINE="true", AUTOTRADE_WATCH_MINTS=MINT)
    assert cli.main(["providers", "health"], e) == 3
    assert cli.main(["ingest", "--once"], e) == 3
    assert cli.main(["doctor", "--network"], e) == 0
    assert "skipped" in capsys.readouterr().out


def test_ingest_requires_targets_and_db(tmp_path, capsys):
    assert cli.main(["ingest", "--once"], env(tmp_path)) == 3
    assert "nothing to ingest" in capsys.readouterr().out
    assert cli.main(["ingest", "--once"], env(tmp_path, AUTOTRADE_WATCH_MINTS=MINT)) == 1


def patch_rpc(monkeypatch, clock, sleeper, handler):
    monkeypatch.setattr(
        cli, "build_rpc", lambda config: make_rpc(rpc_transport(handler), clock, sleeper)
    )


def test_ingest_once_end_to_end_with_mocked_rpc(tmp_path, monkeypatch, capsys, clock, sleeper):
    def handler(m, p):
        if m == "getAccountInfo":
            return {"context": {"slot": 1}, "value": mint_account()}
        return [{"signature": sig(1), "slot": 5, "err": None, "blockTime": 1767268800}]

    patch_rpc(monkeypatch, clock, sleeper, handler)
    e = env(tmp_path, AUTOTRADE_WATCH_MINTS=MINT, AUTOTRADE_WATCH_WALLETS=WALLET)
    cli.main(["db", "init"], e)
    capsys.readouterr()
    assert cli.main(["ingest", "--once"], e) == 0
    out = capsys.readouterr().out
    assert "token_metadata: success" in out and "wallet_activity: success" in out
    assert cli.main(["ingest", "--once"], e) == 0  # idempotent re-run
    assert "duplicates=1" in capsys.readouterr().out


def test_ingest_once_failure_exit_code(tmp_path, monkeypatch, capsys, clock, sleeper):
    import httpx

    patch_rpc(monkeypatch, clock, sleeper, lambda m, p: httpx.Response(503))
    e = env(tmp_path, AUTOTRADE_WATCH_MINTS=MINT)
    cli.main(["db", "init"], e)
    capsys.readouterr()
    assert cli.main(["ingest", "--once"], e) == 1
    assert "failed" in capsys.readouterr().out


def test_providers_health_with_mocked_rpc(tmp_path, monkeypatch, capsys, clock, sleeper):
    patch_rpc(monkeypatch, clock, sleeper, lambda m, p: 123)
    e = env(tmp_path)
    cli.main(["db", "init"], e)
    assert cli.main(["providers", "health"], e) == 0
    assert "status=healthy" in capsys.readouterr().out
    cli.main(["status"], e)
    assert "provider solana_rpc: healthy" in capsys.readouterr().out


def test_providers_health_down_is_exit_1(tmp_path, monkeypatch, capsys, clock, sleeper):
    import httpx

    patch_rpc(monkeypatch, clock, sleeper, lambda m, p: httpx.Response(503))
    assert cli.main(["providers", "health"], env(tmp_path)) == 1


def test_console_entrypoint_runs_as_subprocess(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "autotrade.cli", "doctor"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "", "AUTOTRADE_OFFLINE": "true"},
    )
    assert proc.returncode == 0 and "live execution=LOCKED" in proc.stdout


@pytest.mark.parametrize("cmd", ["buy", "sell", "trade", "sign", "send"])
def test_no_trading_commands_exist(tmp_path, cmd):
    assert cli.main([cmd], env(tmp_path)) == 2


def test_ingest_watch_stops_gracefully(tmp_path, monkeypatch, capsys, clock, sleeper):
    import asyncio

    patch_rpc(
        monkeypatch, clock, sleeper, lambda m, p: {"context": {"slot": 1}, "value": mint_account()}
    )
    real_poll = cli.poll

    async def poll_then_stop(engine, jobs, interval, stop, max_cycles=None):
        asyncio.get_running_loop().call_later(0.1, stop.set)
        return await real_poll(engine, jobs, interval, stop, max_cycles)

    monkeypatch.setattr(cli, "poll", poll_then_stop)
    e = env(tmp_path, AUTOTRADE_WATCH_MINTS=MINT)
    cli.main(["db", "init"], e)
    capsys.readouterr()
    assert cli.main(["ingest", "--watch"], e) == 0
    assert "token_metadata: success" in capsys.readouterr().out


def test_ingest_requires_current_schema(tmp_path, capsys):
    import sqlite3

    sqlite3.connect(tmp_path / "cli.db").close()  # empty db file, no migrations
    assert cli.main(["ingest", "--once"], env(tmp_path, AUTOTRADE_WATCH_MINTS=MINT)) == 1
    assert "out of date" in capsys.readouterr().err


def test_dotenv_loaded_by_cli(tmp_path, monkeypatch, capsys):
    import os

    (tmp_path / ".env").write_text(f"AUTOTRADE_DB_PATH={tmp_path / 'from_env_file.db'}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})
    assert cli.main(["status"]) == 0
    assert "from_env_file.db" in capsys.readouterr().out
