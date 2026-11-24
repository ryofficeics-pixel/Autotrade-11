# Acceptance Criteria

## Safety
- Default PAPER even with missing environment variables.
- No seed phrase/private key/API secret in Git, logs, fixtures, or prompts.
- Live unavailable by default and cannot be enabled by AI/strategy output.
- All order intents pass deterministic risk gate.
- Unknown critical data, stale quotes, provider failure, and ledger errors block entries.
- Kill switch works through restart/failure tests.

## Data
- Every observation records source, observed_at, received_at, and quality.
- Duplicate/out-of-order events do not corrupt state.
- Timeouts/retries/rate limits bounded.
- Missing data is UNKNOWN, never safe zero.
- Recorded fixtures replay deterministically.

## Stage 1 status (data)
- Met (tests): provenance + quality on every record; duplicate/out-of-order safe; bounded retries/timeouts/rate limits; unknown is explicit (`authorities_known`, freshness UNKNOWN); failed ingestion preserves data; no signing/broadcast code.
- Not met: recorded-fixture deterministic *replay* (no replay harness yet); live RPC verification.

## Backtest status (M5)
- Met (tests): fees, slippage, impact, latency, failed transactions, no-fill/missed fills, stop gaps modelled; dead tokens written down; walk-forward and a locked, contamination-tracked holdout; net expectancy, profit factor, max drawdown, tail loss, turnover, fee share, benchmark reported; identical data/config/code yields identical hash; no look-ahead tested.
- Not met: results on real multi-day history (none exists yet); partial fills are not modelled (fills are all-or-nothing); universe survivorship depends on what was polled.

## Strategy
- Discovery is not a buy signal.
- Signals are versioned, expire, and include reason codes.
- No future-data leakage.
- Wallet labels evidence-based and evaluated out of sample.
- Strategy cannot bypass safety/risk.

## Risk
- Unit tests cover every limit, boundary, and interaction.
- Sizing includes stop distance and round-trip costs.
- Exposure, cash reserve, concurrency, and daily stop enforced.
- Duplicate intents idempotently rejected.
- Circuit breakers cover stale data, provider failure, ledger mismatch, execution errors.
- Every decision is auditable/replayable.

## Backtest
- Models fees, slippage, impact, priority fees, latency, failures, partial/no fills.
- Universe includes dead/failed tokens where data exists; limitations disclosed.
- Walk-forward and untouched holdout supported.
- Report net expectancy, profit factor, max drawdown, tail loss, turnover, fee share.
- Same data/config/code yields identical output.

## Paper
- No transaction signed or broadcast.
- Conservative executable-price fills.
- Cash + positions + realized/unrealized PnL reconcile.
- Restart recovery does not duplicate orders.
- Reports include rejected signals and missed/failed fills.

## Paper status (M6)
- Met (tests): no transaction signed or broadcast (no such code path; source scan); conservative executable-price fills; cash + positions + realized/unrealized PnL reconcile and mismatches halt entries; restart recovery does not duplicate orders; reports include rejected signals and missed/failed fills.
- Not met: 30 consecutive paper days (M8); live data collection over time.

## Future live release gate
- 30 consecutive paper days.
- Minimum sample and statistical test plan pre-registered.
- Positive net expectancy on validation and holdout after conservative costs.
- Drawdown within approved budget.
- No unresolved critical/high security or reconciliation issue.
- Signer and transaction integration independently reviewed.
- Fee and loss budget explicitly approved.
- User explicitly authorizes live mode.

Passing criteria does not guarantee future profits.
