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
| token_program | classic SPL Token, or Token-2022 when `AUTOTRADE_SAFETY_ALLOW_TOKEN_2022` is true (default); anything else fails | RPC |
| token_extensions | classic: none by definition. Token-2022: every extension is benign (metadata/pointer/group/immutableOwner/memo/cpiGuard/closeAuthority) or harmless (no permanent delegate, no transfer-hook program, not pausable by a live authority, default account state not frozen, transfer fee ≤ `AUTOTRADE_SAFETY_MAX_TRANSFER_FEE_BPS` (100) with no authority able to change it). Dangerous ⇒ FAIL; unrecognised/unanalysed ⇒ UNKNOWN (reject) | RPC (on-chain) |
| mint_authority_revoked / freeze_authority_revoked | authority is null on-chain | RPC |
| market_data_fresh | newest DEX Screener batch ≤ `AUTOTRADE_MAX_SNAPSHOT_AGE_SECONDS` | DEX Screener |
| liquidity | best-pool reported liquidity ≥ `min_liquidity_usd` (default 10,000) | provider-reported |
| pool_age | oldest pair ≥ `min_pool_age_minutes` (default 30; confirmation delay) | provider-reported |
| holder_concentration | Jupiter top-holders % ≤ 40 (unit inferred as percent from live samples; docs unreadable) | provider-reported |
| sellability_evidence | observed sells 24h ≥ 20, 1h ≥ 1, sell/buy ≥ 0.10 (evidence, not proof; no honeypot simulation) | provider-reported |
| sell_route | a fresh read-only Jupiter quote (`GET /swap/v1/quote`, token → wSOL, probe ≈ `AUTOTRADE_SAFETY_SELL_PROBE_USD`, default $5) returns a route with positive output; "no route" (`TOKEN_NOT_TRADABLE` etc.) FAILs; missing/stale quote is UNKNOWN. Evidence of a sell path, **not** proof (no transaction is simulated or sent); the reported price impact has an unverified unit and is stored, never gated | provider-reported |
| price_cross_check | recent DEX Screener vs Jupiter price within tolerance | cross-check |
| provider_vs_onchain | no discrepancy between Jupiter claims and RPC | cross-check |

Every evaluation is appended to `safety_evaluations` with verdict, per-rule observed/threshold, the policy used, and the row ids it read (snapshot ids, intel id, cross-check ids): fully traceable. Stale data never passes (stale ⇒ FAIL/UNKNOWN). Thresholds come from `AUTOTRADE_SAFETY_*` env vars.

CLI: `autotrade ingest` (discovery + ingestion + cross-checks + screening, prints verdicts), `autotrade candidates`, `autotrade safety [--mint M] [-v]` (offline re-evaluation of stored data; exit 1 if any rejects).

Live check (2026-11-24): discovery pulled 6 real Solana candidates; all 6 were rejected (most use Token-2022; others failed price cross-check, pool age or had unknown liquidity). Jupiter returned HTTP 429 once at 1 rps and the run was reported PARTIAL, as designed. Strict defaults are intentional: expect few PASS results (a live run after adding extension analysis produced one genuine PASS among six real candidates).

Token-2022 extensions are analysed (live `getAccountInfo` jsonParsed shape verified; fee/delegate/hook/pausable shapes follow the Solana docs and are fixture-tested only). The `tokenMetadata` extension also gives verified on-chain symbol/name. A 25-minute real collection run (17 cycles, discovery on) found and fixed two issues: wSOL cannot be quoted against itself (Jupiter 400 `CIRCULAR_ARBITRAGE_IS_DISABLED`), so wSOL is never probed; and request-scoped 4xx answers (400/404/422) no longer mark a provider degraded (401/403/429/5xx still do), because one bad target had been degrading Jupiter every cycle and would have blocked the risk gate. Ruleset `m2-2` adds `sell_route` (live quote verified: 200 with `inAmount/outAmount/routePlan[].swapInfo.label`; 400 `{errorCode}` for no route). Not in M2: true honeypot/transfer simulation (would need transaction simulation), holder-level on-chain concentration (uses Jupiter's provider-reported figure), creator/dev history, wallet observation (M3).

## M3: features, wallet flows, signals (stage-3-features-signals)
Research output only: **a signal is a database row, not an order.** `strategies/` and `features/` import no execution, broker, signer or network code (tested).

- **Wallet flows** (`data/providers/tx_parse.py`): for watched wallets, recorded successful signatures are interpreted via `getTransaction` (bounded by `AUTOTRADE_MAX_TX_PARSE_PER_CYCLE`, default 10). Method: the wallet's net change per mint (`meta.pre/postTokenBalances` filtered by `owner`) plus native lamports (fee added back for the fee payer). A single non-quote token moving while SOL/wSOL/USDC/USDT moves the opposite way = swap (buy if token up, sell if down); quote moves under a crumb threshold are ignored; token moving without payment = `transfer`; failed txs = `failed`; token-for-token or unexpected shapes = `unparsed` (never guessed). Stored in `wallet_tx_parsed` (marker, prevents refetch) and `wallet_flows`. Verified against a real mainnet swap (fixture `solana_swap_tx.json`) and live: 3 swaps + 5 transfers classified from 8 real transactions. `getTransaction` now requests `maxSupportedTransactionVersion: 1` (the node returned error -32015 for version-1 transactions with `0`); still-unsupported versions become rejections, not crashes.
- **Features** (`features/momentum.py`, version `f1`, table `feature_sets`): `ret_5m/15m/1h`, `txn_rate_accel`, `imbalance_5m/1h`, `liq_drawdown_1h`, `range_1h`, `price_usd`, `liquidity_usd`, `wallet_buyers_1h/sellers_1h`. **Point-in-time:** every query is bounded by `observed_at <= as_of` (wallet flows also by `received_at <= as_of`), so later rows cannot change an earlier result (tested). No interpolation: a reference snapshot must exist near the target time, otherwise the feature is unknown (None + note). First useful features need ~15 minutes of continuous polling; `ret_1h` needs ~1 hour.
- **Strategy `hybrid-v1`** (`strategies/hybrid.py`): requires the latest M2 safety verdict = PASS and recent (`AUTOTRADE_SIGNAL_MAX_SAFETY_AGE_SECONDS`); then ALL of `MOMENTUM_15M` (ret_15m ≥ 2%), `MOMENTUM_5M_UP`, `BUY_PRESSURE_5M` (imbalance ≥ 0.05), `ACTIVITY_ACCELERATING` (5m rate ×12 / 1h count ≥ 1), `LIQUIDITY_STABLE` (drawdown ≤ 10%). `WALLET_BUYERS` adds score but is never required (wallet labels are hypotheses). Unknown input ⇒ no signal (`UNKNOWN:<feature>`); a failed condition ⇒ `FAILED:<code>`.
- **Signals** (`signals` table): reason codes, score, `expires_at` (default 5 min; CHECK expires > created), invalidation levels (price/liquidity floors, safety must remain PASS), `feature_as_of` and `safety_evaluation_id` for traceability; unique per (mint, strategy, feature time) so reruns are idempotent. Expiry is by time; expired rows stay for audit.
- **CLI**: `ingest` now ends each cycle with cross-checks, safety verdicts and signal decisions; `autotrade signals [--all]`, `autotrade features --mint M`.

Limits: thresholds are untuned defaults (no backtest yet, M5); the signal has no position sizing or stop logic (M4); swap interpretation covers wallet-vs-quote swaps only; wallet performance is not evaluated, so wallets are context, not edge. Thresholds via `AUTOTRADE_SIGNAL_*`.

## M4: deterministic risk gate (stage-4-risk-engine)
`risk/engine.py` is the final authority between a research signal and a **paper intent**. It is pure over (stored data, config, portfolio, clock): same inputs ⇒ same decision. It imports no network, provider, broker or signer code (tested). **An approved decision is a database row; no order is placed, simulated or sent in M4** (the paper broker is M6).

Every rule is evaluated and stored (PASS/FAIL/UNKNOWN, observed value, threshold); unknown rejects. Reason code = first failing rule in this order: `mode_paper`, `kill_switch`, `signal_unexpired`, `safety_pass` (latest M2 verdict PASS and fresh), `providers_healthy` (rpc/dexscreener/jupiter healthy within `AUTOTRADE_RISK_PROVIDER_MAX_AGE_SECONDS`), `quote_fresh` (DEX Screener snapshot ≤ 5 s old at decision time), `quote_consistent` (recent price cross-check ok), `daily_loss_stop` (halts at ≤ −2% of start-of-day equity), `max_positions` (3), `no_averaging_down`, `duplicate_intent`, `cooldown`, `fx_rate`, `stop_distance`, `cash_reserve` (70% stays unallocated), `exposure_room` (30% cap), `order_size`, `fixed_costs`, `price_impact`, `slippage`, `edge_vs_costs`.

Sizing follows `docs/RISK_POLICY.md`: N ≤ E·r / (d + c) with d = stop distance from the signal's invalidation price and c = round-trip DEX fees + price impact (2N/L, constant-product approximation) + fixed network fees; solved exactly (positive root of a quadratic, Decimal). N is then limited by position cap (10%), cash above reserve, remaining aggregate exposure, and the liquidity/impact cap, and must exceed the minimum order and the fixed-cost ceiling. Leverage is structurally zero (N ≤ free cash).

Explicit inputs, never invented: `AUTOTRADE_USD_IDR_RATE` has **no default** (equity is IDR, prices are USD; unset ⇒ every intent is rejected with `FX_RATE`). `AUTOTRADE_RISK_ASSUMED_EDGE_FRACTION` is an explicit *hypothesis* about expected edge per trade (no validated edge exists until the M5 backtest and M8 paper validation); unset ⇒ `EDGE_VS_COSTS` rejects. Fee/network-cost defaults (0.30% per side, 0.02 USD per transaction) are assumptions, tunable via `AUTOTRADE_RISK_*`.

Controls: persistent **kill switch** (`risk_state`; `autotrade risk kill-switch on|off|status`; any unrecognised stored value halts; survives restarts). **Circuit breaker:** an engine exception engages the kill switch automatically, returns `ENGINE_ERROR`, and stays halted until a human releases it; provider outage/stale quote/inconsistent price reject through their rules. **Idempotency:** one approval per (mint, strategy, feature time), enforced by a partial unique index; a cooldown (default 1 h) blocks re-entry in the same mint.

Audit: `risk_decisions` (append-only) stores the decision, every rule result, the portfolio snapshot and the snapshot/signal ids used. CLI: `autotrade risk evaluate|decisions|kill-switch`; `ingest` prints `risk APPROVED/REJECTED` per signal.

Limits: before the paper ledger is funded (or without an FX rate) the gate uses an explicit flat portfolio (initial equity, all cash, no positions); since M6 it reads the real paper ledger. Slippage is estimated by price impact. Emergency-exit recovery paths are not built.

## M5: backtesting (stage-5-backtest)
Replays **only data this system collected** (stored DEX Screener snapshots, stored safety verdicts and price cross-checks); nothing is fabricated or back-filled. Needs history first: run `autotrade ingest --watch` for days; with little data the report says so (`sample_warning` below 30 trades: not statistical evidence). Research only: replayed results are not evidence of future returns.

- **No look-ahead:** at tick t the strategy sees features computed from rows observed ≤ t, the latest stored safety evaluation ≤ t (must be PASS and recent), and the latest stored price cross-check ≤ t (must be ok). Decisions fill later: at the first tick of that mint at/after decision + latency (default 15 s, so typically the next poll), at that tick's price. Tested: inserting wild data after the window cannot change the result.
- **Cost model (all conservative, in `BacktestConfig`):** price impact 2N/L each side, plus a 0.5% slippage buffer each side, DEX fee per side, network fee per transaction, deterministic failed-transaction draws (5%, seeded hash; a failed tx still pays the fee), missed fills (no tick in time, impact over the cap, stop already breached), stop gaps (exit at the next observed price, never at the stop level), time stop (1 h), liquidity-collapse exit, **dead tokens** (a position still open when a token stops printing is written down by a haircut, default 100%), and positions still open at the end are marked as censored and flagged.
- **Shared logic, no copies:** sizing is `risk/sizing.py` (also used by the live gate) and momentum is `strategies/hybrid.momentum_check` (also used by live signals). Backtest-only simplifications (documented): provider-health and kill-switch gates cannot be reconstructed and are skipped; the live edge-hypothesis gate is skipped (edge is what is being measured); the daily-loss stop is measured against starting equity; one position per mint; fills use snapshot prices (no order-book path).
- **Report:** trades, net PnL, expectancy per trade (return and USD), win rate, profit factor, max drawdown, worst trade and 5th-percentile trade, turnover, time-weighted exposure, fees and fee share of gross, execution drag, missed/failed/dead/censored counts, exit reasons, and an equal-weight buy-and-hold benchmark; plus a `result_hash`: identical data + config + code ⇒ identical hash (tested).
- **Validation protocol:** `autotrade backtest lock [--fraction 0.2]` fixes the holdout boundary once (later calls cannot move it). `backtest run` (development) can never reach the holdout; `backtest walkforward --folds N` runs contiguous out-of-sample folds on development data (fixed configuration, no fitting; reports per-fold and pooled expectancy and positive folds); `backtest holdout` evaluates the locked window. If the holdout is evaluated with a *different* configuration than an earlier holdout run, all holdout experiments are flagged CONTAMINATED (the holdout was used to choose a configuration). Every run is recorded in `experiments` with config hash, window and result hash (`backtest list`). The config hash covers risk/signal settings, backtest assumptions and strategy/feature/ruleset versions.

Limits: thresholds are still untuned defaults; synthetic prices appear only in tests. Real history from a few days of polling will yield few trades. Survivorship: the universe is whatever was discovered/watched while the system ran (tokens that vanished are included only if they were polled before dying). 

## M6: paper broker and ledger (stage-6-paper-broker)
**Nothing is signed, built or sent.** Fills are arithmetic over stored DEX Screener snapshots. This is the first place orders and fills exist, and they are simulated rows only.

- **Orders:** every APPROVED `risk_decisions` row becomes exactly one pending entry order (`UNIQUE decision_id`, so a restart cannot duplicate it). Orders become executable at `decision + latency` and fill at the **first stored quote at/after that time** (never the decision-time quote); no quote before `max_pending` ⇒ `missed`.
- **Fill model** (`paper/execution_model.py`, shared with the backtester): executable price = quote × (1 + impact + slippage buffer) for buys, × (1 − impact − slippage buffer) for sells; impact = 2N/L; DEX fee per side; network fee per transaction; deterministic seeded failed-transaction draws (a failed tx pays the fee and leaves the position unchanged; a failed exit is retried next cycle); entries are refused (`missed`) if impact exceeds the cap, the quote is already at/below the stop, or paper cash is insufficient. Exits are never refused: a thin pool just fills worse.
- **Exits** (created each cycle for open positions): `stop` (price ≤ stop), `liquidity` (≤ the signal's liquidity floor), `time` (max hold), `dead` (no quote for `dead_after_seconds`: written down by the haircut, default 100%). Stops gap: the fill is at the later observed price, not at the stop.
- **Ledger** (`ledger_entries`, append-only, USD): one idempotent deposit (initial equity ÷ `AUTOTRADE_USD_IDR_RATE`), then buy / sell / fee / failed_tx_fee entries. Cash = sum of entries.
- **Reconciliation** (`autotrade paper reconcile`, and after every cycle): cash recomputed from fills and failed orders must equal the ledger; open quantity per mint must equal bought − sold; every filled order has exactly one fill; `equity − deposit = realized + unrealized + failed fees`. **Any mismatch engages the kill switch** (circuit breaker `ledger mismatch`).
- **Risk gate integration:** the gate now reads the real portfolio from the ledger (`paper/portfolio.py`): equity, cash, open positions, start-of-day equity (recorded once per UTC day) and realized PnL today. So the concurrency, exposure, cash-reserve, no-averaging-down and daily-loss-stop rules are live. Mints with open positions stay in the polling set.
- **Restart recovery:** all state is in SQLite; re-running cycles creates no duplicate orders, fills or ledger rows (tested).
- **Reports:** `autotrade paper status|report|run|reconcile`; `report` lists orders by status, missed/failed reasons, rejected signals by risk reason, realized PnL by exit reason and the reconciliation result. The ingest cycle prints paper events.

Limits: single-currency USD ledger with a fixed FX rate for IDR; fills use snapshot prices (no order-book path, no partial fills); stop/limit orders do not rest anywhere (exits are evaluated each poll, so a stop between polls is only seen at the next poll: this is deliberately pessimistic); mark-to-market uses the last stored quote (shown as `n/a` if none); `paper run` offline can fill using already-stored quotes only.

## M7: operations (stage-7-operations)
See `docs/RUNBOOK.md` for procedures.
- **Health check:** `autotrade healthcheck` exits 0 only if no issue exists: kill switch on, no/old successful ingestion (`AUTOTRADE_HEALTHCHECK_MAX_RUN_AGE_SECONDS`, default 600), provider down/degraded, stale market data, ledger mismatch, DB integrity. Meant for cron/systemd.
- **Alerts:** each ingest cycle turns those issues (and each approved paper intent, as `info`) into alerts: appended as JSON lines to `AUTOTRADE_ALERT_FILE` (default `logs/alerts.log`) and optionally POSTed as `{"text": ...}` to `AUTOTRADE_ALERT_WEBHOOK_URL` (a secret: never logged, stored or shown). Per-key cooldown (default 1 h) suppresses repeats; every alert and suppression is stored in `alerts`. Delivery failures are logged and never break the cycle. Messages are redacted (no URLs/keys).
- **Daily report:** `autotrade report daily [--date]` writes `daily-YYYY-MM-DD.md` (health, ingestion, providers, discovery/screening, signals and risk decisions, paper PnL and reconciliation, alerts) to `AUTOTRADE_REPORT_DIR`.
- **Backups:** `autotrade db backup` uses the SQLite online backup API, verifies the copy with `integrity_check`, and keeps the newest `AUTOTRADE_BACKUP_KEEP` (default 7). Restore steps are printed and in the runbook.
- **Secret scanning:** `scripts/scan_secrets.py` (run in CI) fails on private-key blocks, Solana keypair arrays, well-known token formats, API keys in URLs, and tracked `.env`/key files; it prints file, line and rule only. `# secretscan: allow` exempts a line.
- **Deployment:** systemd units and timer in `deploy/`, plus a crontab alternative. Graceful stop on SIGTERM.

Limits: the exit gate "runs unattended 7 days without manual fix" is **not demonstrated**; alert delivery beyond the local file depends on a user-supplied webhook; no log shipping or metrics server; the scanner is pattern-based and cannot catch every secret.

## M8 tooling: readiness gate (stage-8-token2022-gate)
`autotrade gate` evaluates the "Future live release gate" criteria from stored evidence and prints MET / NOT_MET / UNKNOWN / MANUAL for each: consecutive paper days with successful ingestion, zero ledger mismatches, minimum closed paper trades, positive net expectancy (lower confidence bound of the mean return > 0, pre-registered z), positive expectancy on a non-contaminated holdout with enough trades, realized drawdown within *your* approved budget, no unresolved critical issue (including a failed reconciliation), your approved loss budget, independent review of any signer (always MANUAL: none exists), and your explicit authorisation (always NOT MET here). Thresholds are in `docs/M8_PREREGISTRATION.md`, a draft you must review and freeze. Items that need a value only you can supply stay UNKNOWN. **The command cannot enable live trading**; live mode remains locked in code. Exit 0 means only that all automatable evidence items are met.

## Database (SQLite, WAL, FK on, UTC)
Timestamps: `YYYY-MM-DDTHH:MM:SS.ffffffZ` text (sortable, CHECK-constrained on `tokens`). Quantities: exact decimal text. Migrations: `schema_migrations(version, name, checksum, applied_at)`; applied migrations are checksummed — drift or unknown versions fail `db init`/`db check`.

| Table | Key / uniqueness | Notes |
|---|---|---|
| wallet_tx_parsed / wallet_flows | PK (wallet,signature) / UNIQUE(wallet,signature,mint) | interpreted swaps |
| feature_sets / signals | UNIQUE(mint,as_of,version) / UNIQUE(mint,strategy,feature_as_of) | point-in-time features; expiring research signals |
| risk_decisions / risk_state | append-only; UNIQUE approved key / halt flag | M4 gate output and kill switch |
| experiments / backtest_lock | append-only / single row | M5 registry and holdout boundary |
| paper_orders / paper_fills / paper_positions / ledger_entries / daily_equity | UNIQUE decision_id; one fill per order; one open position per mint; partial-unique deposit | M6 simulated execution and ledger |
| alerts | index (key,ts) | M7 alert history incl. suppressed repeats |
| sell_quotes | UNIQUE(source,mint,observed_at) | read-only sell-route evidence |
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
