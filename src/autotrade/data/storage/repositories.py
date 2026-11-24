"""Idempotent persistence. Callers wrap these in Database.transaction()."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from autotrade.domain.models import (
    IngestionResult,
    MarketSnapshot,
    Pool,
    ProviderHealth,
    RunStatus,
    Token,
    Wallet,
    WalletActivity,
)
from autotrade.domain.models import (
    _Provenanced as _Prov,
)
from autotrade.domain.timeutil import fmt_ts

TABLES = (
    "tokens",
    "pools",
    "market_snapshots",
    "wallets",
    "wallet_activity",
    "provider_health",
    "ingestion_runs",
    "schema_migrations",
)


class Outcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    DUPLICATE = "duplicate"  # identical/older observation already stored; nothing changed
    CONFLICT = "conflict"  # contradicts immutable stored facts; rejected


@dataclass
class Counts:
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    conflicts: int = 0

    def add(self, outcome: Outcome) -> None:
        match outcome:
            case Outcome.INSERTED:
                self.inserted += 1
            case Outcome.UPDATED:
                self.updated += 1
            case Outcome.DUPLICATE:
                self.duplicates += 1
            case Outcome.CONFLICT:
                self.conflicts += 1


def _prov(m: _Prov) -> tuple[str, str, str, str, str]:
    return (
        m.source,
        fmt_ts(m.observed_at),
        fmt_ts(m.received_at),
        m.quality.value,
        json.dumps(list(m.quality_notes)),
    )


def _opt_ts(v: datetime | None) -> str | None:
    return fmt_ts(v) if v else None


def upsert_token(conn: sqlite3.Connection, t: Token) -> Outcome:
    row = conn.execute(
        "SELECT decimals, observed_at FROM tokens WHERE mint=?", (t.mint,)
    ).fetchone()
    obs = fmt_ts(t.observed_at)
    if row is None:
        conn.execute(
            "INSERT INTO tokens(mint,decimals,symbol,name,token_program,authorities_known,"
            "mint_authority,freeze_authority,supply_raw,first_seen_at,last_seen_at,"
            "source,observed_at,received_at,quality,quality_notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.mint,
                t.decimals,
                t.symbol,
                t.name,
                t.token_program,
                int(t.authorities_known),
                t.mint_authority,
                t.freeze_authority,
                None if t.supply_raw is None else str(t.supply_raw),
                obs,
                obs,
                *_prov(t),
            ),
        )
        return Outcome.INSERTED
    if row["decimals"] != t.decimals:
        return Outcome.CONFLICT  # mint decimals are immutable on-chain
    if obs <= row["observed_at"]:
        return Outcome.DUPLICATE
    conn.execute(
        "UPDATE tokens SET symbol=COALESCE(?,symbol), name=COALESCE(?,name), token_program=?,"
        "authorities_known=?, mint_authority=?, freeze_authority=?, supply_raw=?, last_seen_at=?,"
        "source=?, observed_at=?, received_at=?, quality=?, quality_notes=? WHERE mint=?",
        (
            t.symbol,
            t.name,
            t.token_program,
            int(t.authorities_known),
            t.mint_authority,
            t.freeze_authority,
            None if t.supply_raw is None else str(t.supply_raw),
            obs,
            *_prov(t),
            t.mint,
        ),
    )
    return Outcome.UPDATED


def upsert_pool(conn: sqlite3.Connection, p: Pool) -> Outcome:
    row = conn.execute(
        "SELECT base_mint, quote_mint, observed_at FROM pools WHERE address=?", (p.address,)
    ).fetchone()
    obs = fmt_ts(p.observed_at)
    if row is None:
        conn.execute(
            "INSERT INTO pools(address,dex,base_mint,quote_mint,program_id,created_at,"
            "first_seen_at,last_seen_at,source,observed_at,received_at,quality,quality_notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.address,
                p.dex,
                p.base_mint,
                p.quote_mint,
                p.program_id,
                _opt_ts(p.created_at),
                obs,
                obs,
                *_prov(p),
            ),
        )
        return Outcome.INSERTED
    if (row["base_mint"], row["quote_mint"]) != (p.base_mint, p.quote_mint):
        return Outcome.CONFLICT
    if obs <= row["observed_at"]:
        return Outcome.DUPLICATE
    conn.execute(
        "UPDATE pools SET dex=?, program_id=COALESCE(?,program_id),"
        "created_at=COALESCE(?,created_at), last_seen_at=?, source=?, observed_at=?,"
        "received_at=?, quality=?, quality_notes=? WHERE address=?",
        (p.dex, p.program_id, _opt_ts(p.created_at), obs, *_prov(p), p.address),
    )
    return Outcome.UPDATED


def insert_snapshot(conn: sqlite3.Connection, s: MarketSnapshot) -> Outcome:
    obs = fmt_ts(s.observed_at)
    cur = conn.execute(
        "INSERT OR IGNORE INTO market_snapshots(pool_address,base_mint,quote_mint,price_quote,"
        "price_usd,liquidity_usd,volume_24h_usd,source,observed_at,received_at,quality,"
        "quality_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            s.pool_address,
            s.base_mint,
            s.quote_mint,
            str(s.price_quote),
            None if s.price_usd is None else str(s.price_usd),
            None if s.liquidity_usd is None else str(s.liquidity_usd),
            None if s.volume_24h_usd is None else str(s.volume_24h_usd),
            *_prov(s),
        ),
    )
    if cur.rowcount == 1:
        return Outcome.INSERTED
    existing = conn.execute(
        "SELECT price_quote FROM market_snapshots WHERE source=? AND pool_address=? "
        "AND observed_at=?",
        (s.source, s.pool_address, obs),
    ).fetchone()
    if existing is not None and s.price_quote != Decimal(existing["price_quote"]):
        return Outcome.CONFLICT
    return Outcome.DUPLICATE


def upsert_wallet(conn: sqlite3.Connection, w: Wallet) -> Outcome:
    obs = fmt_ts(w.observed_at)
    row = conn.execute("SELECT observed_at FROM wallets WHERE address=?", (w.address,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO wallets(address,label,first_seen_at,last_seen_at,source,observed_at,"
            "received_at,quality,quality_notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (w.address, w.label, obs, obs, *_prov(w)),
        )
        return Outcome.INSERTED
    if obs <= row["observed_at"]:
        return Outcome.DUPLICATE
    conn.execute(
        "UPDATE wallets SET label=COALESCE(?,label), last_seen_at=?, source=?, observed_at=?,"
        "received_at=?, quality=?, quality_notes=? WHERE address=?",
        (w.label, obs, *_prov(w), w.address),
    )
    return Outcome.UPDATED


def insert_wallet_activity(conn: sqlite3.Connection, a: WalletActivity) -> Outcome:
    cur = conn.execute(
        "INSERT OR IGNORE INTO wallet_activity(wallet_address,signature,slot,block_time,"
        "succeeded,kind,source,observed_at,received_at,quality,quality_notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            a.wallet_address,
            a.signature,
            a.slot,
            _opt_ts(a.block_time),
            int(a.succeeded),
            a.kind,
            *_prov(a),
        ),
    )
    if cur.rowcount == 1:
        return Outcome.INSERTED
    existing = conn.execute(
        "SELECT slot FROM wallet_activity WHERE wallet_address=? AND signature=?",
        (a.wallet_address, a.signature),
    ).fetchone()
    return Outcome.CONFLICT if existing and existing["slot"] != a.slot else Outcome.DUPLICATE


# ---- operational tables ---------------------------------------------------------------
def record_health(conn: sqlite3.Connection, h: ProviderHealth) -> None:
    conn.execute(
        "INSERT INTO provider_health(provider,endpoint,status,checked_at,latency_ms,"
        "consecutive_failures,last_success_at,last_error_kind,last_error_message) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            h.provider,
            h.endpoint,
            h.status.value,
            fmt_ts(h.checked_at),
            h.latency_ms,
            h.consecutive_failures,
            _opt_ts(h.last_success_at),
            h.last_error_kind,
            h.last_error_message,
        ),
    )


def start_run(conn: sqlite3.Connection, job: str, provider: str, started_at: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO ingestion_runs(job,provider,status,started_at) VALUES (?,?,?,?)",
        (job, provider, RunStatus.RUNNING.value, fmt_ts(started_at)),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, r: IngestionResult) -> None:
    conn.execute(
        "UPDATE ingestion_runs SET status=?, finished_at=?, fetched=?, accepted=?, rejected=?,"
        "inserted=?, updated=?, duplicates=?, stale=?, error_kind=?, error_message=? WHERE id=?",
        (
            r.status.value,
            fmt_ts(r.finished_at),
            r.fetched,
            r.accepted,
            r.rejected,
            r.inserted,
            r.updated,
            r.duplicates,
            r.stale,
            r.error_kind,
            r.error_message,
            run_id,
        ),
    )


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608


def latest_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT h.* FROM provider_health h JOIN "
            "(SELECT provider, MAX(id) mid FROM provider_health GROUP BY provider) m "
            "ON h.id = m.mid ORDER BY h.provider"
        )
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,)))


def latest_observed(conn: sqlite3.Connection, table: str) -> str | None:
    if table not in {"market_snapshots", "wallet_activity", "tokens"}:
        raise ValueError(table)
    value: str | None = conn.execute(f"SELECT MAX(observed_at) FROM {table}").fetchone()[0]
    return value
