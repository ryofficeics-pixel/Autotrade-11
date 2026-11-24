"""Idempotent persistence. Callers wrap these in Database.transaction()."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from autotrade.domain.models import (
    Candle,
    CrossCheck,
    DiscoveredToken,
    FeatureSet,
    IngestionResult,
    MarketSnapshot,
    ParsedTransaction,
    Pool,
    ProviderHealth,
    RiskDecision,
    RunStatus,
    SafetyEvaluation,
    Signal,
    Token,
    TokenIntel,
    Wallet,
    WalletActivity,
)
from autotrade.domain.models import (
    _Provenanced as _Prov,
)
from autotrade.domain.timeutil import fmt_ts

TABLES = (
    "risk_decisions",
    "risk_state",
    "wallet_flows",
    "feature_sets",
    "signals",
    "candidates",
    "safety_evaluations",
    "token_intel",
    "ohlcv_candles",
    "cross_checks",
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


def _d(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def insert_snapshot(conn: sqlite3.Connection, s: MarketSnapshot) -> Outcome:
    obs = fmt_ts(s.observed_at)
    cur = conn.execute(
        "INSERT OR IGNORE INTO market_snapshots(pool_address,base_mint,quote_mint,price_quote,"
        "price_usd,liquidity_usd,volume_24h_usd,fdv_usd,market_cap_usd,buys_5m,sells_5m,buys_1h,"
        "sells_1h,buys_24h,sells_24h,pair_created_at,source,observed_at,received_at,quality,"
        "quality_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            s.pool_address,
            s.base_mint,
            s.quote_mint,
            str(s.price_quote),
            _d(s.price_usd),
            _d(s.liquidity_usd),
            _d(s.volume_24h_usd),
            _d(s.fdv_usd),
            _d(s.market_cap_usd),
            s.buys_5m,
            s.sells_5m,
            s.buys_1h,
            s.sells_1h,
            s.buys_24h,
            s.sells_24h,
            _opt_ts(s.pair_created_at),
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


def insert_intel(conn: sqlite3.Connection, t: TokenIntel) -> Outcome:
    cur = conn.execute(
        "INSERT OR IGNORE INTO token_intel(mint,symbol,name,decimals,is_verified,tags,"
        "mint_authority_disabled,freeze_authority_disabled,top_holders_pct,holder_count,"
        "organic_score,usd_price,liquidity_usd,market_cap_usd,fdv_usd,source,observed_at,"
        "received_at,quality,quality_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            t.mint,
            t.symbol,
            t.name,
            t.decimals,
            _b(t.is_verified),
            json.dumps(list(t.tags)),
            _b(t.mint_authority_disabled),
            _b(t.freeze_authority_disabled),
            _d(t.top_holders_pct),
            t.holder_count,
            _d(t.organic_score),
            _d(t.usd_price),
            _d(t.liquidity_usd),
            _d(t.market_cap_usd),
            _d(t.fdv_usd),
            *_prov(t),
        ),
    )
    return Outcome.INSERTED if cur.rowcount == 1 else Outcome.DUPLICATE


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def insert_candle(conn: sqlite3.Connection, c: Candle) -> Outcome:
    cur = conn.execute(
        "INSERT OR IGNORE INTO ohlcv_candles(mint,interval,open_time,open,high,low,close,volume,"
        "source,observed_at,received_at,quality,quality_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            c.mint,
            c.interval,
            fmt_ts(c.open_time),
            str(c.open),
            str(c.high),
            str(c.low),
            str(c.close),
            str(c.volume),
            *_prov(c),
        ),
    )
    if cur.rowcount == 1:
        return Outcome.INSERTED
    row = conn.execute(
        "SELECT close FROM ohlcv_candles WHERE source=? AND mint=? AND interval=? AND open_time=?",
        (c.source, c.mint, c.interval, fmt_ts(c.open_time)),
    ).fetchone()
    # A still-forming candle may be re-reported with new values; keep the first, flag the clash.
    return Outcome.CONFLICT if row and Decimal(row["close"]) != c.close else Outcome.DUPLICATE


def insert_cross_check(conn: sqlite3.Connection, c: CrossCheck) -> None:
    conn.execute(
        "INSERT INTO cross_checks(check_type,mint,status,checked_at,source_a,value_a,source_b,"
        "value_b,deviation,detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            c.check_type,
            c.mint,
            c.status.value,
            fmt_ts(c.checked_at),
            c.source_a,
            c.value_a,
            c.source_b,
            c.value_b,
            _d(c.deviation),
            c.detail,
        ),
    )


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
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


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


def upsert_candidate(conn: sqlite3.Connection, d: DiscoveredToken) -> Outcome:
    ts = fmt_ts(d.received_at)
    row = conn.execute("SELECT 1 FROM candidates WHERE mint=?", (d.mint,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO candidates(mint,feed,source,first_discovered_at,last_discovered_at) "
            "VALUES (?,?,?,?,?)",
            (d.mint, d.feed, d.source, ts, ts),
        )
        return Outcome.INSERTED
    conn.execute(
        "UPDATE candidates SET last_discovered_at=MAX(last_discovered_at,?), "
        "times_seen=times_seen+1 WHERE mint=?",
        (ts, d.mint),
    )
    return Outcome.UPDATED


def candidate_mints(conn: sqlite3.Connection, since: datetime, limit: int) -> list[str]:
    """Newest discovered candidates first-seen at/after `since`."""
    rows = conn.execute(
        "SELECT mint FROM candidates WHERE first_discovered_at >= ? "
        "ORDER BY first_discovered_at DESC LIMIT ?",
        (fmt_ts(since), limit),
    )
    return [r["mint"] for r in rows]


def insert_evaluation(conn: sqlite3.Connection, e: SafetyEvaluation) -> None:
    conn.execute(
        "INSERT INTO safety_evaluations(mint,ruleset_version,evaluated_at,verdict,reasons,rules,"
        "inputs) VALUES (?,?,?,?,?,?,?)",
        (
            e.mint,
            e.ruleset_version,
            fmt_ts(e.evaluated_at),
            e.verdict.value,
            json.dumps(list(e.reasons)),
            json.dumps([r.model_dump(mode="json") for r in e.rules]),
            json.dumps(e.inputs, default=str, sort_keys=True),
        ),
    )


def latest_evaluation(conn: sqlite3.Connection, mint: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM safety_evaluations WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,)
    ).fetchone()
    return row


def insert_parsed_tx(
    conn: sqlite3.Connection, p: ParsedTransaction, parsed_at: datetime
) -> Outcome:
    cur = conn.execute(
        "INSERT OR IGNORE INTO wallet_tx_parsed(wallet_address,signature,kind,parsed_at) "
        "VALUES (?,?,?,?)",
        (p.wallet_address, p.signature, p.kind, fmt_ts(parsed_at)),
    )
    if cur.rowcount == 0:
        return Outcome.DUPLICATE
    for leg in p.flows:
        conn.execute(
            "INSERT OR IGNORE INTO wallet_flows(wallet_address,signature,slot,block_time,mint,side,"
            "token_delta_raw,decimals,counter,counter_delta_raw,source,observed_at,received_at,"
            "quality,quality_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.wallet_address,
                p.signature,
                p.slot,
                _opt_ts(p.block_time),
                leg.mint,
                leg.side.value,
                str(leg.token_delta_raw),
                leg.decimals,
                leg.counter,
                str(leg.counter_delta_raw),
                *_prov(p),
            ),
        )
    return Outcome.INSERTED


def unparsed_signatures(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str]]:
    """Newest successful signatures of known wallets that were not interpreted yet."""
    rows = conn.execute(
        "SELECT a.wallet_address w, a.signature s FROM wallet_activity a "
        "LEFT JOIN wallet_tx_parsed p ON p.wallet_address=a.wallet_address "
        "AND p.signature=a.signature "
        "WHERE a.succeeded=1 AND p.signature IS NULL ORDER BY a.observed_at DESC LIMIT ?",
        (limit,),
    )
    return [(r["w"], r["s"]) for r in rows]


def insert_feature_set(conn: sqlite3.Connection, f: FeatureSet) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO feature_sets(mint,as_of,version,values_json,notes,snapshot_ids) "
        "VALUES (?,?,?,?,?,?)",
        (
            f.mint,
            fmt_ts(f.as_of),
            f.version,
            json.dumps(f.values, sort_keys=True),
            json.dumps(list(f.notes)),
            json.dumps(list(f.snapshot_ids)),
        ),
    )
    row = conn.execute(
        "SELECT id FROM feature_sets WHERE mint=? AND as_of=? AND version=?",
        (f.mint, fmt_ts(f.as_of), f.version),
    ).fetchone()
    return int(row["id"])


def insert_signal(conn: sqlite3.Connection, s: Signal) -> Outcome:
    cur = conn.execute(
        "INSERT OR IGNORE INTO signals(mint,strategy_version,created_at,expires_at,score,reasons,"
        "invalidation,feature_as_of,safety_evaluation_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            s.mint,
            s.strategy_version,
            fmt_ts(s.created_at),
            fmt_ts(s.expires_at),
            s.score,
            json.dumps(list(s.reasons)),
            json.dumps(s.invalidation, sort_keys=True),
            fmt_ts(s.feature_as_of),
            s.safety_evaluation_id,
        ),
    )
    return Outcome.INSERTED if cur.rowcount == 1 else Outcome.DUPLICATE


def active_signals(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM signals WHERE expires_at > ? ORDER BY created_at DESC", (fmt_ts(now),)
        )
    )


def insert_risk_decision(conn: sqlite3.Connection, d: RiskDecision) -> Outcome:
    """Append-only audit row. A second APPROVED row for one idempotency key is impossible
    (partial unique index); that case surfaces as DUPLICATE."""
    try:
        conn.execute(
            "INSERT INTO risk_decisions(signal_id,mint,decided_at,approved,idempotency_key,"
            "reason_code,rules,notional_usd,notional_idr,quantity,entry_price_usd,stop_price_usd,"
            "inputs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.signal_id,
                d.mint,
                fmt_ts(d.decided_at),
                int(d.approved),
                d.idempotency_key,
                d.reason_code,
                json.dumps([r.model_dump(mode="json") for r in d.rules]),
                _d(d.notional_usd),
                _d(d.notional_idr),
                _d(d.quantity),
                _d(d.entry_price_usd),
                _d(d.stop_price_usd),
                json.dumps(d.inputs, default=str, sort_keys=True),
            ),
        )
    except sqlite3.IntegrityError:
        if d.approved:
            return Outcome.DUPLICATE
        raise
    return Outcome.INSERTED


def signal_id_for(conn: sqlite3.Connection, s: Signal) -> int | None:
    row = conn.execute(
        "SELECT id FROM signals WHERE mint=? AND strategy_version=? AND feature_as_of=?",
        (s.mint, s.strategy_version, fmt_ts(s.feature_as_of)),
    ).fetchone()
    return None if row is None else int(row["id"])
