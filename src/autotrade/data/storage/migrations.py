"""Ordered, checksummed schema migrations. Never edit an applied migration; append a new one."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return hashlib.sha256("\n;\n".join(self.statements).encode()).hexdigest()


_PROVENANCE = """
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    quality TEXT NOT NULL CHECK (quality IN ('ok','stale','partial')),
    quality_notes TEXT NOT NULL DEFAULT '[]'"""

_TS = "CHECK (length({c}) = 27 AND {c} LIKE '____-__-__T__:__:__.______Z')"


def _ts(col: str) -> str:
    return _TS.format(c=col)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "initial_schema",
        (
            f"""CREATE TABLE tokens (
    mint TEXT PRIMARY KEY,
    decimals INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    symbol TEXT, name TEXT, token_program TEXT,
    authorities_known INTEGER NOT NULL CHECK (authorities_known IN (0,1)),
    mint_authority TEXT, freeze_authority TEXT,
    supply_raw TEXT CHECK (supply_raw IS NULL OR supply_raw NOT GLOB '*[^0-9]*'),
    first_seen_at TEXT NOT NULL {_ts("first_seen_at")},
    last_seen_at TEXT NOT NULL {_ts("last_seen_at")},{_PROVENANCE},
    {_ts("observed_at")}, {_ts("received_at")}
)""",
            f"""CREATE TABLE pools (
    address TEXT PRIMARY KEY,
    dex TEXT NOT NULL,
    base_mint TEXT NOT NULL, quote_mint TEXT NOT NULL CHECK (quote_mint <> base_mint),
    program_id TEXT,
    created_at TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,{_PROVENANCE}
)""",
            "CREATE INDEX idx_pools_base_mint ON pools(base_mint)",
            f"""CREATE TABLE market_snapshots (
    id INTEGER PRIMARY KEY,
    pool_address TEXT NOT NULL REFERENCES pools(address),
    base_mint TEXT NOT NULL, quote_mint TEXT NOT NULL,
    price_quote TEXT NOT NULL, price_usd TEXT,
    liquidity_usd TEXT, volume_24h_usd TEXT,{_PROVENANCE},
    UNIQUE (source, pool_address, observed_at)
)""",
            "CREATE INDEX idx_snapshots_pool_time ON market_snapshots(pool_address, observed_at)",
            "CREATE INDEX idx_snapshots_mint_time ON market_snapshots(base_mint, observed_at)",
            f"""CREATE TABLE wallets (
    address TEXT PRIMARY KEY,
    label TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,{_PROVENANCE}
)""",
            f"""CREATE TABLE wallet_activity (
    id INTEGER PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    signature TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot >= 0),
    block_time TEXT,
    succeeded INTEGER NOT NULL CHECK (succeeded IN (0,1)),
    kind TEXT NOT NULL,{_PROVENANCE},
    UNIQUE (wallet_address, signature)
)""",
            "CREATE INDEX idx_activity_wallet_time ON wallet_activity(wallet_address, observed_at)",
            "CREATE INDEX idx_activity_slot ON wallet_activity(slot)",
            """CREATE TABLE provider_health (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL, endpoint TEXT,
    status TEXT NOT NULL CHECK (status IN ('healthy','degraded','down','unknown')),
    checked_at TEXT NOT NULL,
    latency_ms REAL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT, last_error_kind TEXT, last_error_message TEXT
)""",
            "CREATE INDEX idx_health_provider_time ON provider_health(provider, checked_at)",
            """CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL, provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    started_at TEXT NOT NULL, finished_at TEXT,
    fetched INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT, error_message TEXT
)""",
            "CREATE INDEX idx_runs_job_time ON ingestion_runs(job, started_at)",
        ),
    ),
)
