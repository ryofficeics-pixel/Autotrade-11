# Master Architecture

## Mission
A Solana hybrid memecoin system combining new-pool discovery, momentum confirmation, and wallet-flow context. Discovery is not a buy signal. The objective is net expectancy, not maximum trade count. The system must be comfortable holding cash.

## Constraints
- Solana market data; devnet for transaction integration tests.
- Initial capital reference: IDR 1,000,000, configurable; never assume all capital is deployable.
- Staged rollout; paper mode only in initial releases.
- Local-first, Python + SQLite, public/free data endpoints where available.
- No leverage, borrowing, or blind copy trading.
- Live execution OFF; no signing key accepted by initial application.

## Component flow
```text
Solana RPC / DEX market feeds / public aggregators
                  |
             Provider adapters
                  |
       Raw events + provenance + UTC
                  |
        Normalize / deduplicate / validate
                  |
             SQLite event store
                  |
       +----------+-----------+
       |                      |
 Token/pool discovery   Wallet-flow observer
       |                      |
       +------ Feature builder+
                  |
       Hybrid strategy signals (versioned)
                  |
        Token & pool safety screening
                  |
          Deterministic risk engine
                  |
             Approved intents
                  |
             Paper broker
                  |
       Ledger / reports / alerts / replay
                  |
       Live broker (future, locked)
```

## Stack and boundaries
- Python 3.12+, asyncio for I/O-bound collectors.
- SQLite WAL initially; migrations; UTC timestamps; append-only events and fills.
- Typed domain models; pytest; structured logs with redaction.
- Provider adapters isolate vendor payloads from domain logic.
- CLI first; dashboard is not a prerequisite.
- Use Decimal or integer atomic units for accounting; never binary float for money.

## Domain records
Token (mint, decimals, authorities, first_seen); Pool (address, DEX/program, mints, reserves, created_at); MarketSnapshot (price, volume, liquidity, imbalance, timestamps, provenance); WalletObservation; FeatureSet (versioned); Signal (strategy version, score, reasons, expiry); RiskDecision; OrderIntent (idempotency key); Fill (requested/executed price, fees, slippage); Position; StrategyRun (code/config/data versions).

## Data ingestion
*Stage 1 status:* implemented for Solana RPC token/wallet data with SQLite, dedupe, freshness, health; DEX Screener and Jupiter adapters are implemented (provider-reported, cross-checked against RPC); Birdeye OHLCV is optional. Details: `docs/DATA_FOUNDATION.md`. The rest of this section is the target design.

Build isolated adapters for Solana RPC, DEX/pool feeds, price/volume aggregators, and wallet watchlists. Record source, observed_at, received_at, latency, and quality. Deduplicate events; reject malformed, stale, contradictory, or out-of-order snapshots. Bound timeouts/retries and honor rate limits. Missing data is UNKNOWN, never zero risk. Required feed degradation blocks new entries.

## Hybrid strategy
1. **Discovery:** observe new mints/pools; wait for minimum data quality and confirmation.
2. **Safety:** check mint/freeze authority where applicable, token program, pool liquidity and changes, holder concentration when reliable, suspicious transfer patterns, and sellability evidence. Unknown critical risk = reject.
3. **Momentum:** configurable short/medium returns, volume acceleration, trade count, buy/sell imbalance, liquidity stability, and volatility.
4. **Wallet context:** track candidate wallets with time-aligned after-cost results; wallet labels are hypotheses, not facts.
5. **Signal:** versioned, expires, includes rationale and invalidation. Strategy cannot submit orders directly.

## Risk engine
Independent deterministic final gate. Checks token/pool safety, max position and aggregate exposure, concurrency, daily drawdown, liquidity/price impact, quote age/deviation, slippage/fee budget, duplicate intents, cooldowns, provider health, and kill switch. Initial paper defaults:
- Risk budget/trade 0.5% equity.
- Position notional cap 10% equity.
- Aggregate exposure cap 30% equity.
- Max 3 concurrent positions.
- Daily loss stop 2% of start-of-day equity.
- Max execution quote age 5 seconds.
- Max slippage 1.0%, plus separate impact cap.
- Keep at least 70% equity unallocated.
- No leverage or averaging down.

Sizing approximation: N <= (equity × risk fraction) / (stop distance + estimated round-trip costs). Then apply position, exposure, cash, liquidity, and price-impact caps. If stop distance or costs are unknown, reject. Stops cannot guarantee fills in illiquid tokens. Tiny account economics may make trades non-viable; do not relax controls to force activity.

## Backtesting
Event-driven replay; no look-ahead. Model latency, spread, slippage/impact by order size vs liquidity, DEX/priority fees, failed transaction costs, partial/no fills, migration, exit liquidity, dead tokens, data gaps, and survivorship bias. Support walk-forward and untouched holdout. Report net PnL, expectancy, profit factor, max drawdown, tail loss, turnover, exposure, trade count, fee share, and benchmark. Win rate alone is insufficient.

## Paper trading
Consume live data but never sign/broadcast. Use executable quotes or conservative fill assumptions, not mid-price. Persist signal/decision/quote/fill times, fees, slippage, rejection/exit reasons. Reconcile cash + positions + realized/unrealized PnL. Recover safely after restart.

## Live execution (future only)
No live order path in foundation. Later require explicit opt-in, separate low-balance hot wallet, isolated signer, transaction simulation, mint/destination allowlists, bounded compute/priority fees, fresh quote, signature/settlement verification, idempotency, reconciliation, kill switch, and independent security review. Never place seed phrase in config, logs, Git, or AI prompts.

## AI boundary
AI can summarize and propose research hypotheses only. It cannot sign, access keys, bypass risk, mark unknown data safe, alter risk limits, or deploy strategy changes. All decisions must be reproducible from versioned data/config/code.

## ADRs
- Python + SQLite local-first.
- Provider adapters prevent vendor lock-in.
- Paper-only default; live disabled.
- Deterministic risk engine is final authority.
- Net-of-cost validation is mandatory.
