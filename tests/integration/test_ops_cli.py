"""M7 operations through the CLI: health check, daily report, backup, alerts from the cycle."""

import json
import sqlite3

from autotrade import cli
from tests.integration.test_discovery_cli import NEW, env, run_cli
from tests.integration.test_risk_cli import RISK_ENV, prepared


def ops_env(tmp_path, **extra):
    return env(
        tmp_path,
        AUTOTRADE_ALERT_FILE=str(tmp_path / "alerts.log"),
        AUTOTRADE_BACKUP_DIR=str(tmp_path / "backups"),
        AUTOTRADE_REPORT_DIR=str(tmp_path / "reports"),
        **extra,
    )


def test_healthcheck_exit_codes(tmp_path, capsys):
    e = ops_env(tmp_path)
    assert cli.main(["healthcheck"], e) == 1
    assert "database not initialized" in capsys.readouterr().out
    cli.main(["db", "init"], e)
    assert cli.main(["healthcheck"], e) == 1  # nothing has ever run
    assert "ingest_never_ran" in capsys.readouterr().out


def test_healthy_after_a_clean_cycle_and_unhealthy_with_kill_switch(
    tmp_path, monkeypatch, capsys, sleeper
):
    prepared(tmp_path)
    e = ops_env(tmp_path, **RISK_ENV)
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    capsys.readouterr()
    cli.main(["healthcheck"], e)
    out = capsys.readouterr().out
    assert "kill_switch" not in out  # the cycle itself did not halt anything
    assert cli.main(["risk", "kill-switch", "on", "--reason", "drill"], e) == 0
    capsys.readouterr()
    assert cli.main(["healthcheck"], e) == 1
    assert "CRITICAL kill_switch: new entries halted: drill" in capsys.readouterr().out


def test_cycle_raises_alert_for_kill_switch_once_per_cooldown(
    tmp_path, monkeypatch, capsys, sleeper
):
    prepared(tmp_path)
    e = ops_env(tmp_path, **RISK_ENV)
    cli.main(["risk", "kill-switch", "on", "--reason", "drill"], e)
    for _ in range(2):
        run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    entries = [json.loads(x) for x in (tmp_path / "alerts.log").read_text().splitlines()]
    kill = [x for x in entries if x["key"] == "kill_switch"]
    assert len(kill) == 1 and kill[0]["severity"] == "critical"
    db = sqlite3.connect(tmp_path / "d.db")
    assert (
        db.execute(
            "SELECT COUNT(*) FROM alerts WHERE key='kill_switch' AND suppressed=1"
        ).fetchone()[0]
        >= 1
    )


def test_approved_intent_raises_info_alert(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    e = ops_env(tmp_path, **RISK_ENV)
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    entries = [json.loads(x) for x in (tmp_path / "alerts.log").read_text().splitlines()]
    assert any(x["key"].startswith("intent_") and x["severity"] == "info" for x in entries)
    assert NEW[:8] in json.dumps(entries)


def test_report_and_backup_commands(tmp_path, capsys):
    e = ops_env(tmp_path)
    cli.main(["db", "init"], e)
    capsys.readouterr()
    assert cli.main(["report", "daily"], e) == 0
    out = capsys.readouterr().out
    assert "# Autotrade-11 daily report" in out and "Live trading: LOCKED" in out
    assert list((tmp_path / "reports").glob("daily-*.md"))
    assert cli.main(["report", "daily", "--date", "2026-01-02"], e) == 0
    assert (tmp_path / "reports" / "daily-2026-01-02.md").exists()
    assert cli.main(["report", "daily", "--date", "nope"], e) == 2
    assert cli.main(["db", "backup"], e) == 0
    out = capsys.readouterr().out
    assert "backup written and verified" in out and "autotrade db check" in out
    assert len(list((tmp_path / "backups").glob("autotrade-*.sqlite3"))) == 1


def test_backup_and_report_need_a_database(tmp_path):
    e = ops_env(tmp_path)
    assert cli.main(["db", "backup"], e) == 1
    assert cli.main(["report", "daily"], e) == 1
