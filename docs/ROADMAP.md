# Implementation Roadmap

## Stage 0 — Audit and foundation
- [x] Audit repository and document limitations.
- [x] Preserve main; create dedicated foundation branch.
- [x] Architecture, risk policy, roadmap, acceptance contracts.
- [ ] CI, lint/type checks, dependency lock, secret scanning.
- [ ] Typed config and SQLite migrations.
Exit: clean structure, domain contracts, CI green, no secrets.

## Stage 1 — Data foundation
Provider interfaces; raw event capture; normalized token/pool/quote/trade/wallet records; SQLite migrations; deduplication; rate limits/backoff; freshness and health checks.
Exit: deterministic replay; provider failure never creates a buy signal.

## Stage 2 — Discovery and safety
New mint/pool discovery; authority/liquidity/concentration/sellability checks; momentum features; wallet observation; versioned signals.
Exit: all signals traceable; unknown critical safety fields rejected.

## Stage 3 — Backtesting
Event replay; conservative cost/fill models; survivorship-bias controls; walk-forward and holdout; experiment registry.
Exit: reproducible net-of-cost results, untouched holdout.

## Stage 4 — Paper trading
Paper broker; portfolio ledger; deterministic risk engine; circuit breakers; daily reports; restart reconciliation.
Exit: 30 consecutive calendar days paper, zero unexplained ledger mismatch, risk tests passing, minimum sample fixed in advance, positive net expectancy on validation and holdout after conservative costs. This is a research gate, not a guarantee.

## Stage 5 — Live readiness (not authorized)
Isolated signer; transaction simulation and route validation; bounded fees/slippage; live kill switch; monitoring and incident runbook; independent security review.
Exit: every gate passed, fee/loss budget approved, and explicit user authorization. Live remains OFF until then.
