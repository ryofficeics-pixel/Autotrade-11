# Milestones to Fully Operational (paper), then gated live

Durations are rough estimates for one developer. "Operational" = PAPER trading running unattended with reconciled ledger. Live is a separate, user-authorized gate (M9). No profit is promised.

| # | Milestone | Deliverables | Exit gate |
|---|---|---|---|
| M0 | Merge Stage 1 | PR #2 reviewed, CI green on 3.12/3.13, **live RPC verified** locally (`autotrade providers health`, `ingest --once` on a real mint/wallet) | CI green; real RPC round trip logged |
| M1 | Market data (**DEX Screener/Jupiter/Birdeye adapters done on `stage-1-dex-data-sources`; retention, raw-event capture, new-token discovery remain**) | DEX/price provider (pick one public API, verify its contract, record fixtures), pool + snapshot ingestion, retention/pruning for health/runs, raw-event capture | Snapshots ingest idempotently; stale/failed feed blocks downstream |
| M2 | Discovery + safety (**implemented: profiles discovery, screening rules incl. Token-2022 extension analysis, audit trail; honeypot simulation not done**) | New mint/pool discovery, safety checks (mint/freeze authority, liquidity, holder concentration, sellability), UNKNOWN = reject | Every candidate traceable; unknown critical field rejected |
| M3 | Features + signals (**implemented on `stage-3-features-signals`; thresholds untuned until M5 backtest**) | Versioned feature set (momentum, volume, imbalance, liquidity stability), wallet-flow parsing (swap-level), expiring signals with reason codes | No look-ahead (tested); signals never place orders |
| M4 | Risk engine (**implemented; reads the M6 ledger portfolio**) | Deterministic gate per `RISK_POLICY.md`: sizing, caps, daily stop, circuit breakers, kill switch, idempotent intents | 100% boundary tests; decisions auditable |
| M5 | Backtest (**implemented on `stage-5-backtest`; needs days of collected history before results mean anything**) | Event replay, fee/slippage/impact/latency/failed-tx models, walk-forward + untouched holdout, dead-token coverage | Reproducible net-of-cost report; holdout untouched |
| M6 | Paper broker + ledger (**implemented on `stage-6-paper-broker`; simulated only**) | Conservative fills, cash/position/PnL reconciliation, restart recovery, rejected/missed-fill reports | Zero unexplained ledger mismatch in replay and restart tests |
| M7 | Operations (**implemented on `stage-7-operations`; 7-day unattended run still to be demonstrated**) | Scheduler/service (systemd or cron), alerts (local/free), daily report, secret scanning in CI, runbook, backups | Runs unattended 7 days without manual fix |
| M8 | Paper validation (**tooling done: `autotrade gate` + draft pre-registration; the 30-day run itself still has to happen**) | 30 consecutive days paper, sample size and tests pre-registered, validation + holdout positive net expectancy after conservative costs | Gate in `ACCEPTANCE_CRITERIA.md` met |
| M9 | Live readiness (not authorized) | Isolated signer, simulation, route validation, fee/slippage bounds, live kill switch, independent security review | Every gate passed + explicit user authorization; live stays OFF until then |

## Next step (do now)
1. Merge PR #2 after review.
2. Run locally: `autotrade db init && autotrade providers health` then `AUTOTRADE_WATCH_MINTS=<mint> autotrade ingest --once`; report output.
3. Start M1: choose the DEX data source (candidates must be checked against their live docs; none verified yet).

## Critical path and risks
- M1 → M2 → M3 → M5 → M6 → M8; M4 can run parallel to M3.
- Largest risks: unverified public API limits/terms; tiny-capital economics (fees may erase edge); survivorship bias in backtests; M8 may conclude no positive expectancy — a valid outcome, then stay paper.
