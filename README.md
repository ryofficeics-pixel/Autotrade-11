# Autotrade-11

Solana hybrid memecoin research and paper-trading platform. Initial release is **paper-only**. Live execution is locked.

## Objective
Measure positive net expectancy after slippage, DEX fees, priority fees, failed transactions, and execution delay. No profit is promised.

## Current status
- [x] Repository baseline audited
- [x] Architecture and safety contracts defined
- [x] Stage 1 data foundation: Solana RPC client, normalized models, SQLite store, ingestion, CLI (read-only; see [Data foundation](docs/DATA_FOUNDATION.md))
- [x] DEX data sources: DEX Screener (primary), Jupiter (secondary), optional Birdeye OHLCV, cross-provider checks
- [x] M2 discovery + safety screening (read-only; verdicts are not signals)
- [x] M3 features, swap-level wallet flows, expiring research signals (no orders)
- [ ] Risk engine implementation
- [ ] Backtest engine
- [ ] Paper broker and portfolio ledger
- [ ] Live execution (disabled; future stage only)

## Safe defaults
- Mode: PAPER
- Live trading: OFF
- Initial capital reference: IDR 1,000,000 (configurable)
- No leverage, borrowing, or averaging down
- Unknown critical risk data = reject
- No secrets or wallet seed phrases in Git

## Cost model
Local Python + SQLite, public/free endpoints where available. No paid subscription or hosted service is required by design. Public API limits can change; fail closed when feeds are unavailable. Electricity, network, and on-chain fees are not literally free.

## Quick start
```bash
pip install -c constraints.txt -e ".[dev]"
autotrade db init && autotrade doctor
autotrade status
AUTOTRADE_WATCH_MINTS=<mint> autotrade ingest --once   # read-only; never trades
```
Commands: `doctor`, `status`, `db init|check`, `providers health`, `ingest --once|--watch`. Exit codes: 0 ok, 1 failed, 2 usage/config, 3 skipped.

## Docs
- [Data foundation (providers, schema, ingestion, setup, troubleshooting)](docs/DATA_FOUNDATION.md)
- [Audit baseline](docs/AUDIT_BASELINE.md)
- [Master architecture](docs/MASTER_ARCHITECTURE.md)
- [Risk policy](docs/RISK_POLICY.md)
- [Roadmap](docs/ROADMAP.md)
- [Acceptance criteria](docs/ACCEPTANCE_CRITERIA.md)
- [Implementation contract](docs/IMPLEMENTATION_CONTRACT.md)

Build sequence: data → research/backtest → paper → gated live. Backtest performance is not proof of future returns.
