"""Conservative simulated execution shared by the paper broker and the backtester.

Pure arithmetic over a snapshot price and liquidity: price impact 2N/L plus a slippage buffer
(always against us), DEX fee per side, a network fee per transaction, and a deterministic seeded
failed-transaction draw. Nothing here signs, builds or sends a transaction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ZERO, ONE = Decimal(0), Decimal(1)


@dataclass(frozen=True)
class FillParams:
    dex_fee_fraction: Decimal
    network_fee_usd: Decimal
    slippage_buffer: Decimal
    max_impact: Decimal


@dataclass(frozen=True)
class BuyFill:
    fill_price: Decimal
    qty: Decimal
    entry_cost: Decimal  # DEX fee + network fee, paid in addition to the notional
    impact: Decimal


@dataclass(frozen=True)
class SellFill:
    fill_price: Decimal
    proceeds: Decimal  # before fees
    fee: Decimal
    impact: Decimal


def buy_fill(mid: Decimal, liq: Decimal, notional: Decimal, p: FillParams) -> BuyFill | None:
    """None when impact exceeds the cap (the order would be refused rather than filled)."""
    if liq <= 0 or mid <= 0 or notional <= 0:
        return None
    impact = 2 * notional / liq
    if impact > p.max_impact:
        return None
    fill = mid * (ONE + impact + p.slippage_buffer)
    return BuyFill(fill, notional / fill, notional * p.dex_fee_fraction + p.network_fee_usd, impact)


def sell_fill(
    mid: Decimal, liq: Decimal, qty: Decimal, p: FillParams, *, haircut: Decimal | None = None
) -> SellFill:
    """Sells always execute (an exit is never refused); a thin pool just fills worse.
    `haircut` models a dead token: a flat write-down with no network fee (nothing is sent)."""
    if haircut is not None:
        fill = mid * (ONE - haircut)
        proceeds = qty * fill
        return SellFill(fill, proceeds, proceeds * p.dex_fee_fraction, ONE)
    proceeds_mid = qty * mid
    impact = min(ONE, 2 * proceeds_mid / liq) if liq > 0 else ONE
    fill = mid * max(ZERO, ONE - impact - p.slippage_buffer)
    proceeds = qty * fill
    return SellFill(fill, proceeds, proceeds * p.dex_fee_fraction + p.network_fee_usd, impact)


def tx_fails(seed: str, mint: str, t: datetime, kind: str, rate: Decimal) -> bool:
    """Deterministic pseudo-random failed-transaction draw (same inputs => same outcome)."""
    h = hashlib.sha256(f"{seed}|{mint}|{t.isoformat()}|{kind}".encode()).digest()
    return Decimal(int.from_bytes(h[:8], "big")) / Decimal(2**64) < rate
