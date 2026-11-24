# Implementation Roadmap

## Stage 0 — Audit and foundation
- [x] Audit repository and document limitations.
- [x] Preserve main; create dedicated foundation branch.
- [x] Architecture, risk policy, roadmap, acceptance contracts.
- [x] CI (ruff, mypy, pytest), pinned constraints, typed config and SQLite migrations (Stage 1 PR).
- [ ] Secret scanning.
Exit: clean structure, domain contracts, CI green, no secrets.

## Stage 1 — Data foundation (implemented; pending merge and live-RPC verification)
Done: provider contracts, Solana RPC client, normalized models, SQLite migrations/dedup, ingestion engine, health/freshness, CLI, tests, CI. DEX Screener/Jupiter/Birdeye adapters added (see DATA_FOUNDATION.md). Not done: new-token discovery endpoints, raw event capture, replay harness, retention.
Provider interfaces; raw event capture; normalized token/pool/quote/trade/wallet records; SQLite migrations; deduplication; rate limits/backoff; freshness and health checks.
Exit: deterministic replay; provider failure never creates a buy signal.

## Stage 2 — Discovery and safety (implemented, see DATA_FOUNDATION.md)
Done: discovery candidates, deterministic screening with unknown=reject, traceable evaluations. Not done: momentum features and wallet observation (M3), Token-2022 extension and honeypot analysis.
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
