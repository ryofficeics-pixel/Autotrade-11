import asyncio
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autotrade.config import AppConfig, OpsSettings
from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.domain.models import HealthStatus, ProviderHealth
from autotrade.ops import alerts
from autotrade.ops.alerts import Alert, emit
from autotrade.ops.backup import BackupError, backup
from autotrade.ops.health import collect_issues
from autotrade.ops.report import build_daily_report
from autotrade.ops.secretscan import scan_paths, scan_text, tracked_files
from autotrade.paper import ledger
from autotrade.risk import state
from tests.conftest import T0

ROOT = Path(__file__).resolve().parents[2]


def ops(tmp_path, **kw) -> OpsSettings:
    return OpsSettings(alert_file=tmp_path / "alerts.log", **kw)


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


# ---- alerts ----
def test_alert_is_written_and_rate_limited_per_key(db, clock, tmp_path):
    o = ops(tmp_path, alert_cooldown_seconds=600)
    a = Alert("kill_switch", "critical", "halted")
    with db.session() as conn:
        assert emit(conn, clock.now, o, a) is True
        assert emit(conn, clock.now + timedelta(seconds=10), o, a) is False  # suppressed
        assert emit(conn, clock.now + timedelta(seconds=10), o, Alert("other", "info", "x"))
        assert emit(conn, clock.now + timedelta(seconds=601), o, a) is True
        rows = conn.execute("SELECT key, suppressed FROM alerts ORDER BY id").fetchall()
    assert [(r["key"], r["suppressed"]) for r in rows] == [
        ("kill_switch", 0),
        ("kill_switch", 1),
        ("other", 0),
        ("kill_switch", 0),
    ]
    assert len(lines(tmp_path / "alerts.log")) == 3  # suppressed alerts are not delivered


def test_alert_messages_are_redacted_and_severity_validated(db, clock, tmp_path):
    o = ops(tmp_path)
    with db.session() as conn:
        emit(
            conn,
            clock.now,
            o,
            Alert("k", "warning", "failed https://rpc.test/?api-key=abcdefghijklmnop1234"),
        )
        with pytest.raises(ValueError):
            emit(conn, clock.now, o, Alert("k2", "fatal", "x"))
    text = (tmp_path / "alerts.log").read_text()
    assert "abcdefghijklmnop1234" not in text and "rpc.test" not in text


def test_webhook_failure_never_breaks_the_cycle_and_secret_is_not_stored(
    db, clock, tmp_path, monkeypatch
):
    sent = []
    monkeypatch.setattr(alerts, "_post_webhook", lambda url, text: sent.append((url, text)))
    o = ops(tmp_path, alert_webhook_url="https://hooks.example.test/T000/SECRETPATH")
    with db.session() as conn:
        assert emit(conn, clock.now, o, Alert("a", "critical", "boom"))
        assert sent and sent[0][1] == "[critical] a: boom"

        def broken(url, text):
            raise alerts.ProviderError("down")

        monkeypatch.setattr(alerts, "_post_webhook", broken)
        assert emit(conn, clock.now, o, Alert("b", "critical", "boom2"))  # still delivered to file
        rows = [dict(r) for r in conn.execute("SELECT * FROM alerts")]
    assert [r["delivered"] for r in rows] == ["file,webhook", "file"]
    assert "SECRETPATH" not in json.dumps(rows) and "SECRETPATH" not in repr(o)


def test_webhook_delivery_works_inside_a_running_event_loop(db, clock, tmp_path, monkeypatch):
    calls = []

    class FakeHttp:
        def __init__(self, *a, **k): ...

        async def post_json(self, url, payload):
            calls.append((url, payload))

        async def aclose(self): ...

    monkeypatch.setattr(alerts, "ResilientHttp", FakeHttp)
    o = ops(tmp_path, alert_webhook_url="https://hooks.example.test/x")

    async def inside_loop():
        with db.session() as conn:
            return emit(conn, clock.now, o, Alert("loop", "warning", "from a coroutine"))

    assert asyncio.run(inside_loop()) is True
    assert calls == [("https://hooks.example.test/x", {"text": "[warning] loop: from a coroutine"})]


def test_ops_config_validation_and_env():
    c = AppConfig.from_env(
        {
            "AUTOTRADE_BACKUP_KEEP": "3",
            "AUTOTRADE_ALERT_COOLDOWN_SECONDS": "60",
            "AUTOTRADE_ALERT_WEBHOOK_URL": "https://hooks.example.test/x",
        }
    )
    assert c.ops.backup_keep == 3 and "hooks.example" not in repr(c) + c.model_dump_json()
    for bad in ({"AUTOTRADE_BACKUP_KEEP": "0"}, {"AUTOTRADE_ALERT_WEBHOOK_URL": "ftp://x"}):
        with pytest.raises(ValueError):
            AppConfig.from_env(bad)


# ---- health ----
def keys(db, now, config=None):
    with db.session() as conn:
        return {i.key: i for i in collect_issues(conn, now, config or AppConfig())}


def test_fresh_database_reports_missing_ingestion(db, clock):
    assert set(keys(db, clock.now)) == {"ingest_never_ran"}


def run_row(db, finished):
    with db.session() as conn:
        rid = repo.start_run(conn, "token_metadata", "p", finished - timedelta(seconds=1))
        conn.execute(
            "UPDATE ingestion_runs SET status='success', finished_at=? WHERE id=?",
            (finished.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), rid),
        )


def test_healthy_when_recent_and_stale_when_old(db, clock):
    run_row(db, clock.now - timedelta(seconds=60))
    assert keys(db, clock.now) == {}
    later = clock.now + timedelta(seconds=1000)
    assert keys(db, later)["ingest_stale"].severity == "critical"


def test_kill_switch_provider_and_ledger_issues(db, clock):
    run_row(db, clock.now)
    with db.session() as conn, db.transaction(conn):
        state.engage(conn, clock.now, "test")
        repo.record_health(
            conn,
            ProviderHealth(
                provider="jupiter",
                status=HealthStatus.DOWN,
                checked_at=clock.now,
                last_error_kind="timeout",
            ),
        )
        repo.record_health(
            conn,
            ProviderHealth(
                provider="dexscreener", status=HealthStatus.DEGRADED, checked_at=clock.now
            ),
        )
        ledger.ensure_deposit(conn, clock.now, Decimal(10))
    found = keys(db, clock.now)
    assert found["kill_switch"].severity == "critical"
    assert found["provider_jupiter"].severity == "critical"
    assert found["provider_dexscreener"].severity == "warning"
    assert "ledger_mismatch" not in found
    with db.session() as conn:  # an unexplained ledger entry: cash no longer matches the fills
        conn.execute("INSERT INTO ledger_entries(ts,kind,amount_usd) VALUES ('t','fee','-1')")
    assert keys(db, clock.now)["ledger_mismatch"].severity == "critical"


def test_stale_market_data_is_flagged(db, clock):
    from tests.unit.test_backtest import ramp, seed

    run_row(db, clock.now)
    seed(db, ramp(5), start=clock.now - timedelta(hours=2))
    assert "market_data_stale" in keys(db, clock.now)


# ---- backups ----
def test_backup_is_verified_restorable_and_pruned(tmp_path, clock):
    path = tmp_path / "live.sqlite3"
    d = Database(path, clock=clock)
    with d.session() as conn:
        d.migrate(conn)
        conn.execute(
            "INSERT INTO wallets(address,first_seen_at,last_seen_at,source,observed_at,"
            "received_at,quality) VALUES ('w','t','t','s','t','t','ok')"
        )
    made = [backup(path, tmp_path / "bk", T0 + timedelta(minutes=i), keep=2) for i in range(4)]
    kept = sorted((tmp_path / "bk").glob("autotrade-*.sqlite3"))
    assert kept == sorted(made[-2:]) and len(kept) == 2
    restored = Database(kept[-1])
    with restored.session(create=False) as conn:
        assert restored.integrity_check(conn).ok
        assert conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1


def test_backup_while_writing_and_errors(tmp_path, clock):
    path = tmp_path / "live.sqlite3"
    d = Database(path, clock=clock)
    with d.session() as conn:
        d.migrate(conn)
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO wallets(address,first_seen_at,last_seen_at,source,observed_at,"
            "received_at,quality) VALUES ('w','t','t','s','t','t','ok')"
        )
        target = backup(path, tmp_path / "bk", clock.now, keep=1)  # uncommitted write not captured
        conn.execute("COMMIT")
    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    check.close()
    with pytest.raises(BackupError):
        backup(tmp_path / "missing.sqlite3", tmp_path / "bk", clock.now, keep=1)


# ---- report ----
def test_daily_report_sections_and_day_bounds(db, clock):
    run_row(db, clock.now)
    with db.session() as conn, db.transaction(conn):
        ledger.ensure_deposit(conn, clock.now, Decimal("62.5"))
        emit(
            conn, clock.now, OpsSettings(alert_file=Path("/dev/null")), Alert("x", "info", "hello")
        )
    with db.session() as conn:
        text = build_daily_report(conn, clock.now.date(), clock.now, AppConfig())
        other = build_daily_report(
            conn, clock.now.date() + timedelta(days=1), clock.now, AppConfig()
        )
    for section in (
        "## Health",
        "## Ingestion runs",
        "## Providers (latest)",
        "## Discovery",
        "## Signals and risk decisions",
        "## Paper trading",
        "## Alerts",
        "Live trading: LOCKED",
        "reconciliation: ok",
        "cash_usd=62.500000",
    ):
        assert section in text
    assert "| x |" not in other and "hello" not in other
    assert "info | x" in text


# ---- secret scanner ----
def test_scanner_detects_high_confidence_secrets():
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----"
    keypair = str(list(range(64)))
    aws = "AK" + "IA" + "A" * 16
    gh = "gh" + "p_" + "a" * 36
    url = "https://rpc.example.test/?api-" + "key=" + "a1b2c3d4e5f6g7h8i9j0k1"
    b58 = "private_key = '" + "1" * 88 + "'"
    for text, rule in [
        (pem, "private-key-block"),
        (keypair, "solana-keypair-array"),
        (aws, "aws-access-key"),
        (gh, "github-token"),
        (url, "key-in-url"),
        (b58, "base58-secret-assignment"),
    ]:
        assert [f.rule for f in scan_text("f", text)] == [rule]


def test_scanner_has_no_false_positives_on_normal_content_and_honours_allow_marker():
    sig = "4Vkx58wX1NQyUh5SWuhNmGy6aLVEN3WxX7Ga8HcNpyrX1eNVgTSbxgqKr8tfbptUkzPPFZkK2rmSV1KgZ3mBcFE4"
    ok = [
        "signature = '" + sig + "'",
        "api_key: SecretStr | None = None",
        "BIRDEYE_API_KEY=",
        "[1, 2, 3]",
        "SOLANA_RPC_URL=https://api.mainnet-beta.solana.com",
    ]
    assert all(scan_text("f", t) == [] for t in ok)
    assert scan_text("f", "-----BEGIN " + "PRIVATE KEY-----  # secretscan: allow") == []


def test_scanner_flags_tracked_env_and_key_files(tmp_path):
    (tmp_path / ".env").write_text("A=1")
    (tmp_path / "x.pem").write_text("nothing")
    found = scan_paths(tmp_path, [".env", "x.pem"])
    assert {f.rule for f in found} == {"forbidden-tracked-file"}


def test_repository_itself_is_clean():
    assert scan_paths(ROOT, tracked_files(ROOT)) == []
