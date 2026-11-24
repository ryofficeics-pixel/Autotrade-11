# Risk Policy — Paper Defaults

These are engineering guardrails, not a guarantee against loss.

Implementation status: enforced by `risk/engine.py` (M4); see `docs/DATA_FOUNDATION.md` for rule order, sizing and the explicit FX/edge inputs.

| Control | Initial value | Breach action |
|---|---:|---|
| Mode | PAPER | Reject live |
| Risk/trade | 0.5% equity | Reject/reduce |
| Position cap | 10% equity | Reject/reduce |
| Aggregate exposure | 30% equity | Reject |
| Concurrent positions | 3 | Reject |
| Daily loss stop | 2% start-of-day equity | Halt new entries |
| Slippage cap | 1.0% | Reject quote |
| Cash reserve | 70% equity | Reject |
| Leverage | 0 | Reject |
| Averaging down | Disabled | Reject |
| Unknown critical data | Reject | No trade |
| Stale/failed critical feed | Reject | Circuit breaker |

## Position sizing
Let E = equity, r = risk fraction, d = estimated adverse move to planned exit, c = estimated round-trip fees + priority fees + slippage as a fraction of notional.

N <= (E × r) / (d + c)

Then apply the smaller of this result, per-position cap, available cash after reserve, and liquidity/impact cap. If d or c is unknown, reject. This is an approximation; realized losses can exceed estimates.

## Reject conditions
Unverified mint/pool; unknown or failing critical authority/sellability checks; low or withdrawing liquidity; excessive impact; stale/inconsistent quote; degraded required provider; duplicate intent; position/daily limits breached; kill switch; edge not exceeding conservative round-trip costs plus safety margin; uneconomic minimum order size.

## Circuit breakers
Halt new entries on daily drawdown, stale data/provider outage, repeated quote/transaction failures, ledger mismatch, timestamp anomalies, unexpected pool migration, risk engine exception, or database write failure. Emergency exits need a separately controlled recovery path.

## Audit trail
Persist timestamp, signal and strategy version, portfolio snapshot, market snapshot IDs, every rule/threshold/observed value/result, and final reason code. Every decision must be explainable and replayable.
