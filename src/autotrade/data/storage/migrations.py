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


_V2_SNAPSHOT_COLUMNS = (
    "fdv_usd TEXT",
    "market_cap_usd TEXT",
    "buys_5m INTEGER",
    "sells_5m INTEGER",
    "buys_1h INTEGER",
    "sells_1h INTEGER",
    "buys_24h INTEGER",
    "sells_24h INTEGER",
    "pair_created_at TEXT",
)

MIGRATIONS = (
    *MIGRATIONS,
    Migration(
        2,
        "dex_data_sources",
        (
            *(f"ALTER TABLE market_snapshots ADD COLUMN {c}" for c in _V2_SNAPSHOT_COLUMNS),
            f"""CREATE TABLE token_intel (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT, name TEXT, decimals INTEGER,
    is_verified INTEGER CHECK (is_verified IN (0,1)),
    tags TEXT NOT NULL DEFAULT '[]',
    mint_authority_disabled INTEGER CHECK (mint_authority_disabled IN (0,1)),
    freeze_authority_disabled INTEGER CHECK (freeze_authority_disabled IN (0,1)),
    top_holders_pct TEXT, holder_count INTEGER, organic_score TEXT,
    usd_price TEXT, liquidity_usd TEXT, market_cap_usd TEXT, fdv_usd TEXT,{_PROVENANCE},
    UNIQUE (source, mint, observed_at)
)""",
            "CREATE INDEX idx_intel_mint_time ON token_intel(mint, observed_at)",
            f"""CREATE TABLE ohlcv_candles (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL, interval TEXT NOT NULL, open_time TEXT NOT NULL,
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
    volume TEXT NOT NULL,{_PROVENANCE},
    UNIQUE (source, mint, interval, open_time)
)""",
            "CREATE INDEX idx_candles_mint_time ON ohlcv_candles(mint, interval, open_time)",
            """CREATE TABLE cross_checks (
    id INTEGER PRIMARY KEY,
    check_type TEXT NOT NULL, mint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','discrepancy','unavailable')),
    checked_at TEXT NOT NULL,
    source_a TEXT, value_a TEXT, source_b TEXT, value_b TEXT, deviation TEXT, detail TEXT
)""",
            "CREATE INDEX idx_checks_mint_time ON cross_checks(mint, check_type, checked_at)",
        ),
    ),
)


MIGRATIONS = (
    *MIGRATIONS,
    Migration(
        3,
        "discovery_and_safety",
        (
            """CREATE TABLE candidates (
    mint TEXT PRIMARY KEY,
    feed TEXT NOT NULL, source TEXT NOT NULL,
    first_discovered_at TEXT NOT NULL, last_discovered_at TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1 CHECK (times_seen >= 1)
)""",
            "CREATE INDEX idx_candidates_first ON candidates(first_discovered_at)",
            """CREATE TABLE safety_evaluations (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','reject')),
    reasons TEXT NOT NULL,
    rules TEXT NOT NULL,
    inputs TEXT NOT NULL
)""",
            "CREATE INDEX idx_safety_mint_time ON safety_evaluations(mint, evaluated_at)",
        ),
    ),
)


MIGRATIONS = (
    *MIGRATIONS,
    Migration(
        4,
        "features_flows_signals",
        (
            """CREATE TABLE wallet_tx_parsed (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    signature TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('swap','transfer','failed','unparsed')),
    parsed_at TEXT NOT NULL,
    PRIMARY KEY (wallet_address, signature)
)""",
            f"""CREATE TABLE wallet_flows (
    id INTEGER PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    signature TEXT NOT NULL,
    slot INTEGER NOT NULL,
    block_time TEXT,
    mint TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    token_delta_raw TEXT NOT NULL, decimals INTEGER NOT NULL,
    counter TEXT NOT NULL, counter_delta_raw TEXT NOT NULL,{_PROVENANCE},
    UNIQUE (wallet_address, signature, mint)
)""",
            "CREATE INDEX idx_flows_mint_time ON wallet_flows(mint, observed_at)",
            """CREATE TABLE feature_sets (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL, as_of TEXT NOT NULL, version TEXT NOT NULL,
    values_json TEXT NOT NULL, notes TEXT NOT NULL, snapshot_ids TEXT NOT NULL,
    UNIQUE (mint, as_of, version)
)""",
            """CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL, strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL CHECK (expires_at > created_at),
    score INTEGER NOT NULL CHECK (score >= 0),
    reasons TEXT NOT NULL, invalidation TEXT NOT NULL,
    feature_as_of TEXT NOT NULL,
    safety_evaluation_id INTEGER NOT NULL REFERENCES safety_evaluations(id),
    UNIQUE (mint, strategy_version, feature_as_of)
)""",
            "CREATE INDEX idx_signals_expiry ON signals(expires_at)",
        ),
    ),
)


MIGRATIONS = (
    *MIGRATIONS,
    Migration(
        5,
        "risk_engine",
        (
            """CREATE TABLE risk_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
)""",
            """CREATE TABLE risk_decisions (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    mint TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0,1)),
    idempotency_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    rules TEXT NOT NULL,
    notional_usd TEXT, notional_idr TEXT, quantity TEXT,
    entry_price_usd TEXT, stop_price_usd TEXT,
    inputs TEXT NOT NULL,
    CHECK (approved = 0 OR (notional_usd IS NOT NULL AND quantity IS NOT NULL
                            AND stop_price_usd IS NOT NULL))
)""",
            "CREATE UNIQUE INDEX uq_risk_approved_key ON risk_decisions(idempotency_key) "
            "WHERE approved = 1",
            "CREATE INDEX idx_risk_mint_time ON risk_decisions(mint, decided_at)",
        ),
    ),
)


MIGRATIONS = (
    *MIGRATIONS,
    Migration(
        6,
        "backtest_registry",
        (
            """CREATE TABLE backtest_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holdout_start TEXT NOT NULL,
    fraction TEXT NOT NULL,
    locked_at TEXT NOT NULL
)""",
            """CREATE TABLE experiments (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('dev','walkforward','holdout')),
    config_hash TEXT NOT NULL,
    window_from TEXT NOT NULL, window_to TEXT NOT NULL,
    summary TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    contaminated INTEGER NOT NULL DEFAULT 0 CHECK (contaminated IN (0,1))
)""",
            "CREATE INDEX idx_experiments_kind ON experiments(kind, created_at)",
        ),
    ),
)
