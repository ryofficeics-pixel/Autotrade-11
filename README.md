# Autotrade-11

Solana hybrid memecoin research and paper-trading platform. Initial release is **paper-only**. Live execution is locked.

## Objective
Measure positive net expectancy after slippage, DEX fees, priority fees, failed transactions, and execution delay. No profit is promised.

## Current status
- [x] Repository baseline audited
- [x] Architecture and safety contracts defined
- [ ] Data adapters and normalized event store
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

## Docs
- [Audit baseline](docs/AUDIT_BASELINE.md)
- [Master architecture](docs/MASTER_ARCHITECTURE.md)
- [Risk policy](docs/RISK_POLICY.md)
- [Roadmap](docs/ROADMAP.md)
- [Acceptance criteria](docs/ACCEPTANCE_CRITERIA.md)
- [Implementation contract](docs/IMPLEMENTATION_CONTRACT.md)

Build sequence: data → research/backtest → paper → gated live. Backtest performance is not proof of future returns.
