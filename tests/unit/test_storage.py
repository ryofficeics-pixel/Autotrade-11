import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database, MigrationError
from autotrade.data.storage.migrations import Migration
from autotrade.domain.models import (
    MarketSnapshot,
    Pool,
    Token,
    Wallet,
    WalletActivity,
)
from tests.conftest import MINT, MINT2, T0, WALLET, WALLET2, sig

EXPECTED = {
    "tokens",
    "pools",
    "market_snapshots",
    "wallets",
    "wallet_activity",
    "provider_health",
    "ingestion_runs",
    "schema_migrations",
}


def tok(**kw):
    base = dict(
        source="s", observed_at=T0, received_at=T0, mint=MINT, decimals=9, authorities_known=True
    )
    return Token(**{**base, **kw})


def test_migrate_creates_all_tables_and_is_idempotent(tmp_path, clock):
    d = Database(tmp_path / "a.db", clock=clock)
    with d.session() as conn:
        assert d.migrate(conn) == [1, 2]
        assert d.migrate(conn) == []
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED <= names
        assert d.schema_version(conn) == d.latest_version() == 2
        assert d.integrity_check(conn).ok
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_indexes_exist(db):
    with db.session() as conn:
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_snapshots_pool_time", "idx_activity_wallet_time", "idx_runs_job_time"} <= idx


def test_checksum_drift_detected(db):
    with db.session() as conn:
        conn.execute("UPDATE schema_migrations SET checksum='tampered'")
        with pytest.raises(MigrationError):
            db.migrate(conn)
        assert not db.integrity_check(conn).ok


def test_unknown_future_migration_refused(db):
    with db.session() as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (99,'x','y','z')")
        with pytest.raises(MigrationError):
            db.migrate(conn)


def test_failed_migration_rolls_back(tmp_path, clock):
    bad = (Migration(1, "bad", ("CREATE TABLE ok_t (x INTEGER)", "CREATE TABLE ok_t (x INTEGER)")),)
    d = Database(tmp_path / "b.db", clock=clock, migrations=bad)
    with d.session() as conn:
        with pytest.raises(sqlite3.OperationalError):
            d.migrate(conn)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ok_t'").fetchone() is None
        assert d.schema_version(conn) == 0


def test_transaction_rolls_back_on_error(db):
    with db.session() as conn:
        with pytest.raises(RuntimeError):
            with db.transaction(conn):
                repo.upsert_token(conn, tok())
                raise RuntimeError("boom")
        assert repo.table_counts(conn)["tokens"] == 0


def test_token_upsert_idempotent_and_ordering(db):
    with db.session() as conn:
        assert repo.upsert_token(conn, tok()) is repo.Outcome.INSERTED
        assert repo.upsert_token(conn, tok()) is repo.Outcome.DUPLICATE
        newer = tok(
            observed_at=T0.replace(minute=5),
            received_at=T0.replace(minute=5),
            mint_authority=WALLET,
        )
        assert repo.upsert_token(conn, newer) is repo.Outcome.UPDATED
        assert repo.upsert_token(conn, tok()) is repo.Outcome.DUPLICATE  # out-of-order ignored
        row = conn.execute("SELECT * FROM tokens").fetchone()
        assert row["mint_authority"] == WALLET
        assert row["first_seen_at"] == "2026-01-01T12:00:00.000000Z"
        assert repo.table_counts(conn)["tokens"] == 1


def test_token_decimals_conflict_rejected(db):
    with db.session() as conn:
        repo.upsert_token(conn, tok())
        later = tok(decimals=6, observed_at=T0.replace(minute=9), received_at=T0.replace(minute=9))
        assert repo.upsert_token(conn, later) is repo.Outcome.CONFLICT
        assert conn.execute("SELECT decimals FROM tokens").fetchone()[0] == 9


def pool():
    return Pool(
        source="s",
        observed_at=T0,
        received_at=T0,
        address=WALLET,
        dex="d",
        base_mint=MINT,
        quote_mint=MINT2,
    )


def snap(**kw):
    base = dict(
        source="s",
        observed_at=T0,
        received_at=T0,
        pool_address=WALLET,
        base_mint=MINT,
        quote_mint=MINT2,
        price_quote=Decimal("0.123456789012345678"),
    )
    return MarketSnapshot(**{**base, **kw})


def test_snapshot_dedupe_exact_decimal_and_conflict(db):
    with db.session() as conn:
        assert repo.upsert_pool(conn, pool()) is repo.Outcome.INSERTED
        assert repo.insert_snapshot(conn, snap()) is repo.Outcome.INSERTED
        assert repo.insert_snapshot(conn, snap()) is repo.Outcome.DUPLICATE
        assert repo.insert_snapshot(conn, snap(price_quote="9")) is repo.Outcome.CONFLICT
        assert repo.insert_snapshot(conn, snap(source="other")) is repo.Outcome.INSERTED
        rows = conn.execute("SELECT price_quote FROM market_snapshots ORDER BY id").fetchall()
        assert len(rows) == 2 and rows[0][0] == "0.123456789012345678"


def test_snapshot_requires_existing_pool_fk(db):
    with db.session() as conn, pytest.raises(sqlite3.IntegrityError):
        repo.insert_snapshot(conn, snap())


def test_pool_mint_change_is_conflict(db):
    with db.session() as conn:
        repo.upsert_pool(conn, pool())
        changed = (
            replace(
                pool(),
            )
            if False
            else pool().model_copy(
                update={
                    "quote_mint": WALLET2,
                    "observed_at": T0.replace(minute=3),
                    "received_at": T0.replace(minute=3),
                }
            )
        )
        assert repo.upsert_pool(conn, changed) is repo.Outcome.CONFLICT


def test_wallet_and_activity_idempotent(db):
    w = Wallet(source="s", observed_at=T0, received_at=T0, address=WALLET)
    a = WalletActivity(
        source="s",
        observed_at=T0,
        received_at=T0,
        wallet_address=WALLET,
        signature=sig(1),
        slot=5,
        succeeded=True,
    )
    with db.session() as conn:
        assert repo.upsert_wallet(conn, w) is repo.Outcome.INSERTED
        assert repo.insert_wallet_activity(conn, a) is repo.Outcome.INSERTED
        assert repo.insert_wallet_activity(conn, a) is repo.Outcome.DUPLICATE
        bad = a.model_copy(update={"slot": 6})
        assert repo.insert_wallet_activity(conn, bad) is repo.Outcome.CONFLICT
        assert repo.table_counts(conn)["wallet_activity"] == 1


def test_schema_constraints_block_bad_rows(db):
    with db.session() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tokens(mint,decimals,authorities_known,first_seen_at,"
                "last_seen_at,source,observed_at,received_at,quality) "
                "VALUES ('m',300,1,'x','x','s','x','x','ok')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO wallets(address,first_seen_at,last_seen_at,source,"
                "observed_at,received_at,quality) VALUES ('w','t','t','s','t','t','bogus')"
            )


def test_integrity_report_flags_fk_violation(db):
    with db.session() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO wallet_activity(wallet_address,signature,slot,succeeded,kind,"
            "source,observed_at,received_at,quality) VALUES "
            "('ghost','s',1,1,'signature','s','t','t','ok')"
        )
        report = db.integrity_check(conn)
    assert not report.ok and any("foreign_key_check" in p for p in report.problems)


def test_no_secret_columns_in_schema(db):
    with db.session() as conn:
        cols = {r[1].lower() for t in EXPECTED for r in conn.execute(f"PRAGMA table_info({t})")}
    assert not any(
        w in c for c in cols for w in ("secret", "private", "seed", "api_key", "password")
    )
