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
| DEX Screener / Jupiter / Birdeye adapters | see "DEX data sources" |

RPC: primary `SOLANA_RPC_URL` (default public `https://api.mainnet-beta.solana.com`, rate-limited, no SLA). Fallback URLs are tried only after bounded retries fail transiently; logical errors (e.g. invalid params) do not fail over. Methods outside a read-only allowlist are refused locally. Only hosts (never full URLs) appear in health/logs/DB.

## DEX data sources (stage-1-dex-data-sources)
All values below are **provider-reported**; only `tokens` (RPC `getAccountInfo`) is **on-chain verified**. Discovery is never a buy signal; nothing here places orders.

| Provider | Endpoint (verified live 2026-11-24) | Auth / limits | Stored in |
|---|---|---|---|
| DEX Screener (primary) | `GET api.dexscreener.com/tokens/v1/solana/{mint}` → array of pairs (`[]` if unknown) | no key; docs: 300 req/min (we default to 4 rps) | `pools`, `market_snapshots` (price, USD price, liquidity, 24h volume, FDV/mcap if present, buys/sells 5m/1h/24h, pair age) |
| Jupiter (secondary) | `GET lite-api.jup.ag/tokens/v2/search?query={mint}` → array; only exact `id` match accepted | no key worked at test time; optional `JUPITER_API_KEY`; limits not guaranteed (default 1 rps) | `token_intel` (symbol, decimals, verified flag, audit flags, top-holder %, usdPrice, liquidity) |
| Solana RPC (on-chain truth) | see above | public endpoint | `tokens`, `wallet_activity` |
| Birdeye (optional) | `GET public-api.birdeye.so/defi/ohlcv?address&type&time_from&time_to` + `X-API-KEY`, `x-chain: solana` | needs the user's key; disabled without it | `ohlcv_candles` (closed candles only) |

Verification status: DEX Screener, Jupiter and RPC were exercised against the live APIs (health, ingest, re-ingest, cross-checks). `/price/v3` on Jupiter also answered live but is unused (search already returns `usdPrice`). Birdeye: docs read; live call returned `401 Unauthorized` without a key (confirms auth); its **success-response parsing is verified only against a fixture built from the documented example**, not a live capture.

Data rules: DEX Screener has no per-pair update time → `observed_at` = collection time (stale detection there reflects collection lag only). Jupiter uses its own `updatedAt` as `observed_at`. Missing fields stay NULL/unknown (e.g. missing liquidity → `quality=partial`). Only pairs whose *base* token is the watched mint are stored. Snapshots accumulate as history (unique per source+pool+observed_at).

Cross-checks (`cross_checks` table, run after each ingest cycle): `price_usd` (DEX Screener vs Jupiter, tolerance `AUTOTRADE_PRICE_TOLERANCE_FRACTION`), `decimals`, `mint_authority`, `freeze_authority` (Jupiter claim vs on-chain). Status `ok` / `discrepancy` / `unavailable`; a missing or stale input is `unavailable`, never `ok`. Downstream stages must treat `discrepancy` and `unavailable` as unusable.

Graceful degradation: each provider is an independent job; one provider failing yields a FAILED run for that job only, while the others still persist.

## M2: discovery and safety screening (stage-2-discovery-safety)
Read-only; **no signals, no orders, no buy/sell logic.** A discovered mint is only a name on a list; a PASS verdict is only "no rule failed on fresh, verified data".

- **Discovery** (opt-in `AUTOTRADE_DISCOVERY_ENABLED=true`): `GET api.dexscreener.com/token-profiles/latest/v1` (docs: 60 req/min; array of `{chainId, tokenAddress, ...}`, verified live). Solana entries only, capped at `AUTOTRADE_MAX_CANDIDATES_PER_CYCLE`, stored in `candidates` (first/last seen, times seen). Runs before each ingest cycle, so new candidates are ingested (RPC, DEX Screener, Jupiter), cross-checked and screened in the same cycle. Candidates older than `AUTOTRADE_CANDIDATE_MAX_AGE_HOURS` drop out of polling.
- **Screening** (`safety/screening.py`, ruleset `m2-1`), each rule returns PASS / FAIL / UNKNOWN; any non-PASS rejects:

| Rule | PASS when | Source |
|---|---|---|
| onchain_record | verified RPC mint record exists | RPC (on-chain) |
| token_program | classic SPL Token (Token-2022 rejected unless `AUTOTRADE_SAFETY_ALLOW_TOKEN_2022=true`; extensions are not analysed) | RPC |
| mint_authority_revoked / freeze_authority_revoked | authority is null on-chain | RPC |
| market_data_fresh | newest DEX Screener batch ≤ `AUTOTRADE_MAX_SNAPSHOT_AGE_SECONDS` | DEX Screener |
| liquidity | best-pool reported liquidity ≥ `min_liquidity_usd` (default 10,000) | provider-reported |
| pool_age | oldest pair ≥ `min_pool_age_minutes` (default 30; confirmation delay) | provider-reported |
| holder_concentration | Jupiter top-holders % ≤ 40 (unit inferred as percent from live samples; docs unreadable) | provider-reported |
| sellability_evidence | observed sells 24h ≥ 20, 1h ≥ 1, sell/buy ≥ 0.10 (evidence, not proof; no honeypot simulation) | provider-reported |
| price_cross_check | recent DEX Screener vs Jupiter price within tolerance | cross-check |
| provider_vs_onchain | no discrepancy between Jupiter claims and RPC | cross-check |

Every evaluation is appended to `safety_evaluations` with verdict, per-rule observed/threshold, the policy used, and the row ids it read (snapshot ids, intel id, cross-check ids): fully traceable. Stale data never passes (stale ⇒ FAIL/UNKNOWN). Thresholds come from `AUTOTRADE_SAFETY_*` env vars.

CLI: `autotrade ingest` (discovery + ingestion + cross-checks + screening, prints verdicts), `autotrade candidates`, `autotrade safety [--mint M] [-v]` (offline re-evaluation of stored data; exit 1 if any rejects).

Live check (2026-11-24): discovery pulled 6 real Solana candidates; all 6 were rejected (most use Token-2022; others failed price cross-check, pool age or had unknown liquidity). Jupiter returned HTTP 429 once at 1 rps and the run was reported PARTIAL, as designed. Strict defaults are intentional: expect few or no PASS results.

Not in M2: Token-2022 extension analysis, honeypot/transfer simulation, holder-level on-chain concentration (uses Jupiter's provider-reported figure), creator/dev history, wallet observation (M3).

## Database (SQLite, WAL, FK on, UTC)
Timestamps: `YYYY-MM-DDTHH:MM:SS.ffffffZ` text (sortable, CHECK-constrained on `tokens`). Quantities: exact decimal text. Migrations: `schema_migrations(version, name, checksum, applied_at)`; applied migrations are checksummed — drift or unknown versions fail `db init`/`db check`.

| Table | Key / uniqueness | Notes |
|---|---|---|
| candidates | PK mint | discovery feed hits (not verdicts) |
| safety_evaluations | PK id; index (mint,evaluated_at) | append-only verdict + rules + inputs |
| token_intel | UNIQUE(source,mint,observed_at) | provider-reported token facts; append-only |
| ohlcv_candles | UNIQUE(source,mint,interval,open_time) | closed candles only |
| cross_checks | PK id; index (mint,check_type,checked_at) | cross-provider/on-chain results |
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
- DEX Screener/Jupiter only report what they report: no timestamped pair updates (DEX Screener), possibly missing audit flags (Jupiter omitted mint/freeze flags for USDC → `unavailable`). Pair discovery of *new* tokens (token-profiles endpoints) is not implemented; only watch-listed mints are polled.
- Public API limits and terms can change; Jupiter's free `lite-api` host is not guaranteed.
- Token symbol/name are unknown (null); wallet activity is signature-level only.
- `provider_health` and `ingestion_runs` are append-only with no retention; `ingest --watch` grows them every cycle (~1M rows/year at 60s). Prune manually until retention is added.
- Public RPC limits can change; none are guaranteed.
