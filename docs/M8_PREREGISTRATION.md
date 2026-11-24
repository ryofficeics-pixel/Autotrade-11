# M8 paper-validation pre-registration (DRAFT, unfrozen)

Purpose: fix the success test BEFORE collecting the evidence, so the result cannot be tuned after
the fact. **This is a proposal. It becomes binding only when you review it, change what you
disagree with, and commit it unchanged afterwards** (record the commit SHA as the freeze point).
Passing it is a research gate, not a profit guarantee, and live trading stays locked either way.

## Hypothesis
Strategy `hybrid-v1` (features `f1`, safety ruleset `m2-1`, risk gate M4, paper broker M6) has
positive expected net return per paper trade after the modelled costs.

## Frozen items (edit before freezing)
| Item | Proposed value | Where set |
|---|---|---|
| Configuration under test | the exact `.env` + code SHA at freeze (no tuning during the run) | git SHA |
| Minimum calendar days | 30 consecutive UTC days of successful ingestion | `AUTOTRADE_GATE_MIN_DAYS` |
| Minimum closed paper trades | 100 | `AUTOTRADE_GATE_MIN_TRADES` |
| Primary metric | mean net return per closed trade = realized PnL / cost basis (after fees, impact, slippage buffer, failed-tx fees) | `ops/gate.py` |
| Test | one-sided: lower bound of the mean (normal approximation, z = 1.645) must be > 0 | `AUTOTRADE_GATE_CONFIDENCE_Z` |
| Holdout | locked once with `autotrade backtest lock` before any tuning; ≥ 30 holdout trades; one evaluation per configuration; contaminated ⇒ failed | `AUTOTRADE_GATE_MIN_HOLDOUT_TRADES` |
| Drawdown budget (realized) | **you choose** (e.g. 10%) | `AUTOTRADE_GATE_MAX_DRAWDOWN` |
| Fee and loss budget | **you choose** (USD) | `AUTOTRADE_GATE_LOSS_BUDGET_USD` |
| FX rate and assumed edge | **you choose**; recorded at freeze | `AUTOTRADE_USD_IDR_RATE`, `AUTOTRADE_RISK_ASSUMED_EDGE_FRACTION` |

## Rules during the run
1. No parameter, threshold or code change that affects decisions. A change restarts the clock (new freeze).
2. Outages are not excused: a UTC day without a successful ingestion run breaks the consecutive-days count.
3. Every rejected signal and missed/failed fill stays in the data; nothing is deleted or re-run.
4. Any ledger mismatch is a failure to investigate, never to edit away.

## Honest power note
With a typical memecoin per-trade return standard deviation of, say, 8%, detecting a true mean of
+1% per trade with one-sided 5% error and 80% power needs roughly (1.645 + 0.84)² × (0.08 / 0.01)² ≈ 395 trades.
**100 trades only detects a larger effect (~+2%).** A pass at 100 trades is weaker evidence than a
pass at 400; a non-pass at 100 does not prove there is no edge. Choose the minimum trade count with
that trade-off in mind. The system also produces few trades at its strict defaults, so reaching the
count may take much longer than 30 days; the gate then stays UNKNOWN/NOT MET, which is the correct answer.

## Check
`autotrade gate` prints each criterion as MET / NOT_MET / UNKNOWN / MANUAL with evidence. It cannot
enable live mode. Independent review of any future signer/transaction code and your explicit
authorisation are outside the software and are never satisfied by this tool.
