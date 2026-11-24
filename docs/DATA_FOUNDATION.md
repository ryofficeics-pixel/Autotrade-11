# Data Foundation (Stage 1) — as implemented

Read-only, local-first. Nothing here trades, signs, or broadcasts.

## Provider architecture
- Contracts: `data/providers/base.py` — `TokenMetadataProvider`, `WalletActivityProvider`, `DexMarketDataProvider` (Protocols); all expose `name` and `health()`. Results are `FetchResult(items, rejections, fetched)`: invalid items are reported as rejections, never coerced.
- Transport: `data/providers/http.py` — `ResilientHttp`: explicit timeout, bounded attempts (`max_attempts` total), exponential backoff with jitter (capped), client-side rate limiter, `429`/`Retry-After` honoured only up to `backoff_max_seconds` (longer requests are surfaced as `RateLimited`, not waited on). JSON is parsed with `Decimal` floats.
- Error classes (`domain/errors.py`): `ProviderTimeout`, `TransportError`, `RateLimited`, `HttpStatusError` (5xx/408 retryable, other 4xx not), `RpcError` (retryable only for -32005/-32004), `MalformedResponse`, `InvalidRequest`, `ProviderUnavailable`.
- Health: `HealthTracker` — UNKNOWN before use; HEALTHY after success; DEGRADED for 1–2 consecutive failures; DOWN at ≥3 (rate-limited stays DEGRADED). Each call records request/receive timestamps.

| Implementation | Status |
|---|---|
| `SolanaRpcClient` (getSlot, getAccountInfo, getTransaction, getTokenAccountBalance, getSignaturesForAddress) | implemented; fixture-tested against documented response shapes. **Not verified against a live endpoint** (see Limitations) |
| `RpcTokenMetadataProvider` (mint decimals/authorities/supply via `getAccountInfo` jsonParsed) | implemented, fixture-tested |
| `RpcWalletActivityProvider` (signature-level activity via `getSignaturesForAddress`) | implemented, fixture-tested; no swap/flow parsing |
| DEX market-data provider | **contract only.** No DEX API integration ships; jobs/persistence for it are tested with an in-test fake |

RPC: primary `SOLANA_RPC_URL` (default public `https://api.mainnet-beta.solana.com`, rate-limited, no SLA). Fallback URLs are tried only after bounded retries fail transiently; logical errors (e.g. invalid params) do not fail over. Methods outside a read-only allowlist are refused locally. Only hosts (never full URLs) appear in health/logs/DB.

## Database (SQLite, WAL, FK on, UTC)
Timestamps: `YYYY-MM-DDTHH:MM:SS.ffffffZ` text (sortable, CHECK-constrained on `tokens`). Quantities: exact decimal text. Migrations: `schema_migrations(version, name, checksum, applied_at)`; applied migrations are checksummed — drift or unknown versions fail `db init`/`db check`.

| Table | Key / uniqueness | Notes |
|---|---|---|
| tokens | PK mint | latest state; `authorities_known=0` ⇒ authorities UNKNOWN; decimals immutable (change = conflict) |
| pools | PK address | base/quote mint immutable |
| market_snapshots | PK id; UNIQUE(source,pool_address,observed_at); FK pool | append-only; indexes (pool,time), (mint,time); quality ok/stale/partial |
| wallets | PK address | |
| wallet_activity | PK id; UNIQUE(wallet_address,signature); FK wallet | append-only |
| provider_health | PK id; index (provider,checked_at) | append-only history |
| ingestion_runs | PK id; index (job,started_at) | `running` row written first; terminal status after |
| schema_migrations | PK version | |

No secrets are stored. `tokens` holds only latest state (no authority-change history yet).

## Ingestion pipeline
`fetch (bounded concurrency) → validate/normalize (in provider) → dedupe in batch (contradictory duplicates rejected) → classify stale → persist in ONE transaction → record run + provider health`.
- Idempotent: re-ingesting identical data yields `duplicates`, not rows. Older observations never overwrite newer ones.
- A provider failure persists nothing for that target; a DB error rolls back the whole batch (tested with a failing trigger).
- Status: SUCCESS (no problems) / PARTIAL (some accepted, some failed or rejected) / FAILED.
- Snapshots older than `AUTOTRADE_MAX_SNAPSHOT_AGE_SECONDS` at receipt are stored with `quality=stale` and a note; stale data is never presented as fresh. `status` reports freshness FRESH/STALE/UNKNOWN.
- Polling (`ingest --watch`) is bounded by a stop event (SIGINT/SIGTERM); the current cycle completes, no new one starts.

## CLI
`autotrade doctor [--network]`, `status`, `db init`, `db check`, `providers health`, `ingest --once|--watch`.
Exit codes: 0 ok · 1 failed/degraded · 2 usage or invalid config · 3 skipped (offline mode / no targets).
No buy/sell/sign/send commands exist.

## Local setup
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -c constraints.txt -e ".[dev]"
cp .env.example .env            # optional; exported variables take precedence
autotrade db init && autotrade doctor
AUTOTRADE_WATCH_MINTS=<mint> autotrade ingest --once
pytest && ruff check . && mypy
```

## Troubleshooting
| Symptom | Cause / action |
|---|---|
| `providers health` → degraded/down, `rate_limited` | Public RPC throttling; lower `AUTOTRADE_MAX_REQUESTS_PER_SECOND`, add a fallback or your own endpoint |
| `transport ... ProxyError 403` | Sandbox/proxy blocks the host; allow it or set `SOLANA_RPC_URL` |
| exit 2 `CONFIG ERROR` | Invalid env value (bad address/URL, live mode requested) |
| exit 3 | `AUTOTRADE_OFFLINE=true` or no `AUTOTRADE_WATCH_*` set |
| `db check` PROBLEMS | Migration checksum drift or FK violation; restore from backup |
| `schema out of date` | run `autotrade db init` |

## Limitations (honest)
- No live RPC verification was possible in the build sandbox (outbound to the public RPC host was blocked); behaviour against the real API is based on the documented contract and mocked fixtures. Run `autotrade providers health` locally to verify.
- No DEX/price provider implementation, so market snapshots/pools are not populated by `ingest` yet.
- Token symbol/name are unknown (null); wallet activity is signature-level only.
- `provider_health` and `ingestion_runs` are append-only with no retention; `ingest --watch` grows them every cycle (~1M rows/year at 60s). Prune manually until retention is added.
- Public RPC limits can change; none are guaranteed.
