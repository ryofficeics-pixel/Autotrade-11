"""Backtest report. Win rate alone is never sufficient: expectancy, profit factor, drawdown,
tail loss, turnover, exposure, fee share and a benchmark are always reported together."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from autotrade.backtest.engine import Result, Trade

ZERO = Decimal(0)
MIN_TRADES_FOR_EVIDENCE = 30


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(Decimal("0.000001")))


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs, ZERO) / len(xs) if xs else None


def _percentile(xs: list[Decimal], q: Decimal) -> Decimal | None:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not xs:
        return None
    ordered = sorted(xs)
    rank = max(1, int((q * len(ordered)).to_integral_value(rounding="ROUND_CEILING")))
    return ordered[rank - 1]


def max_drawdown(curve: Sequence[tuple[Any, Decimal]]) -> Decimal:
    peak, worst = ZERO, ZERO
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak)
    return worst


def avg_exposure(samples: list[tuple[Any, Decimal]]) -> Decimal:
    """Time-weighted deployed fraction (piecewise constant between samples)."""
    if len(samples) < 2:
        return ZERO
    total = ZERO
    span = ZERO
    for (t0, x0), (t1, _) in zip(samples, samples[1:], strict=False):
        dt = Decimal(str((t1 - t0).total_seconds()))
        total += x0 * dt
        span += dt
    return total / span if span > 0 else ZERO


def summarize(res: Result) -> dict[str, Any]:
    trades: list[Trade] = res.trades
    net = [t.net_pnl for t in trades]
    returns = [t.net_return for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x < 0]
    gross_mid = sum((t.gross_mid_pnl for t in trades), ZERO)
    fees = sum((t.fees for t in trades), ZERO) + res.failed_tx_cost
    net_total = sum(net, ZERO)
    proceeds = sum((t.qty * t.exit_fill for t in trades), ZERO)
    bought = sum((t.notional for t in trades), ZERO)
    summary: dict[str, Any] = {
        "window": [res.start.isoformat(), res.end.isoformat()],
        "mints": res.mints,
        "ticks": res.ticks,
        "decisions": res.decisions,
        "trades": len(trades),
        "missed_fills": res.missed_fills,
        "failed_transactions": res.failed_tx,
        "censored_open_at_end": sum(1 for t in trades if t.censored),
        "dead_token_exits": sum(1 for t in trades if t.exit_reason == "dead"),
        "exit_reasons": dict(sorted(Counter(t.exit_reason for t in trades).items())),
        "initial_equity_usd": _s(res.initial_equity),
        "net_pnl_usd": _s(net_total),
        "net_return_on_equity": _s(net_total / res.initial_equity),
        "gross_mid_pnl_usd": _s(gross_mid),
        "fees_usd": _s(fees),
        "execution_drag_usd": _s(gross_mid - net_total - fees + res.failed_tx_cost),
        "fee_share_of_gross": _s(fees / gross_mid) if gross_mid > 0 else None,
        "expectancy_return_per_trade": _s(_mean(returns)),
        "expectancy_usd_per_trade": _s(_mean(net)),
        "win_rate": _s(Decimal(len(wins)) / len(trades)) if trades else None,
        "profit_factor": _s(sum(wins, ZERO) / -sum(losses, ZERO)) if losses else None,
        "max_drawdown": _s(max_drawdown(res.equity_curve)),
        "worst_trade_return": _s(min(returns)) if returns else None,
        "p5_trade_return": _s(_percentile(returns, Decimal("0.05"))),
        "turnover_x_equity": _s((bought + proceeds) / res.initial_equity),
        "avg_exposure_fraction": _s(avg_exposure(res.exposure_samples)),
        "benchmark_equal_weight_hold_return": _s(res.benchmark_return),
        "sample_warning": None
        if len(trades) >= MIN_TRADES_FOR_EVIDENCE
        else (f"only {len(trades)} trades (< {MIN_TRADES_FOR_EVIDENCE}): not statistical evidence"),
    }
    return summary


def trades_json(res: Result) -> list[dict[str, str]]:
    return [
        {
            "mint": t.mint,
            "opened": t.opened_at.isoformat(),
            "closed": t.closed_at.isoformat(),
            "notional": str(t.notional),
            "net_pnl": str(t.net_pnl),
            "exit": t.exit_reason,
            "censored": str(t.censored),
        }
        for t in res.trades
    ]


def result_hash(summary: dict[str, Any], res: Result) -> str:
    """Identical data + config + code => identical hash (the reproducibility check)."""
    payload = json.dumps({"summary": summary, "trades": trades_json(res)}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
