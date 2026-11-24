"""M4 through the CLI: signal -> risk gate -> auditable paper intent (no order is placed)."""

from autotrade import cli
from tests.integration.test_discovery_cli import NEW, env, rows, run_cli
from tests.integration.test_signals_cli import seed_history

RISK_ENV = {"AUTOTRADE_USD_IDR_RATE": "16000", "AUTOTRADE_RISK_ASSUMED_EDGE_FRACTION": "0.10"}


def ingest(monkeypatch, tmp_path, sleeper, **extra):
    e = env(tmp_path, **{**RISK_ENV, **extra})
    return e, run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))


def prepared(tmp_path):
    cli.main(["db", "init"], env(tmp_path))
    seed_history(tmp_path, {15: "50", 5: "60"})


def test_approved_intent_is_persisted_with_full_audit(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    e, code = ingest(monkeypatch, tmp_path, sleeper)
    out = capsys.readouterr().out
    assert code == 0 and f"risk APPROVED {NEW[:8]}…" in out and "no order is placed" in out
    d = rows(tmp_path, "SELECT * FROM risk_decisions")[0]
    assert d["approved"] == 1 and float(d["notional_usd"]) > 1 and d["stop_price_usd"]
    assert (
        d["idempotency_key"] and "snapshot_id" in d["inputs"] and "providers_healthy" in d["rules"]
    )
    assert cli.main(["risk", "decisions"], e) == 0
    assert "APPROVED APPROVED" in capsys.readouterr().out
    # nothing resembling an order/position exists anywhere
    names = {r["name"] for r in rows(tmp_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {n for n in names if "order" in n or "position" in n or "fill" in n}


def test_second_cycle_is_blocked_by_cooldown(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    ingest(monkeypatch, tmp_path, sleeper)
    capsys.readouterr()
    ingest(monkeypatch, tmp_path, sleeper)
    assert "risk REJECTED" in capsys.readouterr().out
    approvals = rows(tmp_path, "SELECT COUNT(*) n FROM risk_decisions WHERE approved=1")[0]["n"]
    assert approvals == 1


def test_kill_switch_blocks_new_intents_and_persists(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    e = env(tmp_path, **RISK_ENV)
    assert cli.main(["risk", "kill-switch", "on", "--reason", "test"], e) == 0
    assert "kill switch: ON (test)" in capsys.readouterr().out
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    out = capsys.readouterr().out
    assert "risk REJECTED" in out and "KILL_SWITCH" in out
    assert rows(tmp_path, "SELECT COUNT(*) n FROM risk_decisions WHERE approved=1")[0]["n"] == 0
    assert cli.main(["risk", "kill-switch", "status"], e) == 0
    assert "ON" in capsys.readouterr().out
    assert cli.main(["risk", "kill-switch", "off"], e) == 0
    assert "off" in capsys.readouterr().out


def test_missing_fx_rate_rejects_instead_of_guessing(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    e = env(tmp_path)  # no FX, no edge
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    out = capsys.readouterr().out
    assert "risk REJECTED" in out and "FX_RATE" in out
    assert rows(tmp_path, "SELECT COUNT(*) n FROM risk_decisions WHERE approved=1")[0]["n"] == 0


def test_provider_outage_never_yields_an_intent(tmp_path, monkeypatch, capsys, sleeper):
    prepared(tmp_path)
    e = env(tmp_path, AUTOTRADE_WATCH_MINTS=NEW, AUTOTRADE_DISCOVERY_ENABLED="false", **RISK_ENV)
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10), dex_ok=False)
    out = capsys.readouterr().out
    assert "market_snapshots: failed" in out and "risk APPROVED" not in out
    assert "SIGNAL" not in out.replace("no-signal", "")  # stale seeded data cannot drive a signal
    assert rows(tmp_path, "SELECT COUNT(*) n FROM risk_decisions WHERE approved=1")[0]["n"] == 0


def test_risk_evaluate_and_decisions_offline(tmp_path, capsys):
    e = env(tmp_path, **RISK_ENV)
    assert cli.main(["risk", "evaluate"], e) == 1  # no database
    cli.main(["db", "init"], e)
    assert cli.main(["risk", "evaluate"], e) == 3
    assert "no active signals" in capsys.readouterr().out
    assert cli.main(["risk", "decisions"], e) == 0
    assert "no risk decisions" in capsys.readouterr().out
