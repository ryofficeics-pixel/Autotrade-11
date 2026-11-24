"""Pure position sizing shared by the live risk gate and the backtester (one implementation).

N <= E*r / (d + c) with c = round-trip DEX fees + price impact (2N/L) + fixed network fees,
solved exactly, then capped (see risk/engine.py module docstring)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from autotrade.config import RiskLimits

ZERO = Decimal(0)
QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class Sizing:
    stop_distance: Decimal
    cash_free: Decimal
    exposure_room: Decimal
    caps: dict[str, Decimal]
    binding: str
    notional: Decimal  # quantized, may be <= 0 (no room)
    impact: Decimal
    fixed_fraction: Decimal
    round_trip_cost: Decimal  # fraction of notional


def max_size_for_risk(
    equity: Decimal, r: Decimal, d: Decimal, fee: Decimal, fixed: Decimal, liq: Decimal
) -> Decimal:
    """Largest N with N*(d + 2f + 4N/L + 2F/N) <= E*r  (positive root; 0 if none)."""
    budget = equity * r - 2 * fixed
    if budget <= 0:
        return ZERO
    b = d + 2 * fee
    a = 4 / liq
    return (-b + (b * b + 4 * a * budget).sqrt()) / (2 * a)


def compute_sizing(
    price: Decimal,
    stop: Decimal,
    liq: Decimal,
    rk: RiskLimits,
    *,
    equity_usd: Decimal,
    cash_usd: Decimal,
    exposure_usd: Decimal,
) -> Sizing | None:
    """None when the stop distance is not within (0, 1) or liquidity is unusable."""
    if liq <= 0 or price <= 0:
        return None
    d = (price - stop) / price
    if not ZERO < d < 1:
        return None
    cash_free = cash_usd - rk.reserve_cash_fraction * equity_usd
    exposure_room = rk.max_total_exposure_fraction * equity_usd - exposure_usd
    caps = {
        "risk": max_size_for_risk(
            equity_usd, rk.risk_per_trade_fraction, d, rk.dex_fee_fraction, rk.network_fee_usd, liq
        ),
        "position_cap": rk.max_position_fraction * equity_usd,
        "cash_after_reserve": cash_free,
        "exposure_room": exposure_room,
        "impact_cap": rk.max_price_impact_fraction * liq / 2,
    }
    binding = min(caps, key=lambda k: caps[k])
    n = caps[binding]
    if n > 0:
        n = n.quantize(QUANTUM, rounding="ROUND_DOWN")
    fixed = 2 * rk.network_fee_usd / n if n > 0 else Decimal(1)
    impact = 2 * n / liq if n > 0 else ZERO
    cost = 2 * rk.dex_fee_fraction + 2 * impact + fixed
    return Sizing(d, cash_free, exposure_room, caps, binding, n, impact, fixed, cost)
