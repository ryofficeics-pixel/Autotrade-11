"""M3 end-to-end through the CLI: seeded history + mocked providers => a signal is emitted,
listed, expires, and never touches anything execution related."""

from datetime import timedelta

from autotrade import cli
from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.domain.models import MarketSnapshot, Pool
from autotrade.domain.timeutil import utcnow
from tests.integration.test_discovery_cli import NEW, env, rows, run_cli

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"  # pair address used by the DEX fixture


def seed_history(tmp_path, prices):
    db = Database(tmp_path / "d.db")
    now = utcnow()
    with db.session() as conn, db.transaction(conn):
        kw = dict(source="dexscreener", observed_at=now - timedelta(minutes=20), received_at=now)
        repo.upsert_pool(conn, Pool(address=POOL, dex="orca", base_mint=NEW, quote_mint=USDC, **kw))
        for minutes, price in prices.items():
            at = now - timedelta(minutes=minutes)
            repo.insert_snapshot(
                conn,
                MarketSnapshot(
                    pool_address=POOL,
                    base_mint=NEW,
                    quote_mint=USDC,
                    price_quote="1",
                    price_usd=price,
                    liquidity_usd="17302576.45",
                    buys_5m=5,
                    sells_5m=5,
                    buys_1h=50,
                    sells_1h=40,
                    buys_24h=600,
                    sells_24h=500,
                    source="dexscreener",
                    observed_at=at,
                    received_at=at,
                ),
            )


def test_signal_emitted_listed_and_expires(tmp_path, monkeypatch, capsys, sleeper):
    e = env(tmp_path, AUTOTRADE_SIGNAL_TTL_SECONDS="60")
    cli.main(["db", "init"], e)
    seed_history(tmp_path, {15: "50", 5: "60"})  # live mock price is 70.43 => strong uptrend
    code = run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    out = capsys.readouterr().out
    assert code == 0
    assert f"SIGNAL {NEW[:8]}…" in out and "MOMENTUM_15M" in out
    assert "places no orders" in out
    sig = rows(tmp_path, "SELECT * FROM signals")[0]
    assert (
        sig["mint"] == NEW and sig["safety_evaluation_id"] and sig["expires_at"] > sig["created_at"]
    )
    assert cli.main(["signals"], e) == 0
    assert f"ACTIVE {NEW}" in capsys.readouterr().out
    monkeypatch.setattr(cli, "utcnow", lambda: utcnow() + timedelta(seconds=120))
    assert cli.main(["signals"], e) == 0
    assert "no active signals" in capsys.readouterr().out
    assert cli.main(["signals", "--all"], e) == 0
    assert f"EXPIRED {NEW}" in capsys.readouterr().out


def test_no_history_means_no_signal_and_unknown_features(tmp_path, monkeypatch, capsys, sleeper):
    e = env(tmp_path)
    cli.main(["db", "init"], e)
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    out = capsys.readouterr().out
    assert f"no-signal {NEW[:8]}…: UNKNOWN:ret_15m" in out
    assert rows(tmp_path, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0
    assert cli.main(["features", "--mint", NEW], e) == 0
    feats = capsys.readouterr().out
    assert "ret_15m: unknown" in feats and "insufficient history" in feats


def test_weak_momentum_produces_no_signal(tmp_path, monkeypatch, capsys, sleeper):
    e = env(tmp_path)
    cli.main(["db", "init"], e)
    seed_history(tmp_path, {15: "70.4", 5: "70.42"})
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], e=e, m5=(30, 10))
    assert "FAILED:MOMENTUM_15M" in capsys.readouterr().out
    assert rows(tmp_path, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0


def test_signals_and_features_require_database(tmp_path, capsys):
    assert cli.main(["signals"], env(tmp_path)) == 1
    assert cli.main(["features", "--mint", NEW], env(tmp_path)) == 1
