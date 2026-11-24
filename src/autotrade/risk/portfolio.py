"""Portfolio inputs for the risk gate. The paper ledger arrives in M6; until then the only
source is an explicit, empty paper portfolio (no positions, flat day)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Position:
    mint: str
    notional_idr: Decimal  # current mark-to-market value


@dataclass(frozen=True)
class PortfolioState:
    equity_idr: Decimal
    cash_idr: Decimal
    start_of_day_equity_idr: Decimal
    realized_pnl_today_idr: Decimal = Decimal(0)
    positions: tuple[Position, ...] = field(default_factory=tuple)

    @property
    def exposure_idr(self) -> Decimal:
        return sum((p.notional_idr for p in self.positions), Decimal(0))


class PortfolioSource(Protocol):
    def load(self) -> PortfolioState: ...


class FlatPaperPortfolio:
    """Initial equity, all cash, no positions. Honest placeholder until the M6 ledger exists."""

    def __init__(self, equity_idr: Decimal) -> None:
        self._equity = equity_idr

    def load(self) -> PortfolioState:
        return PortfolioState(self._equity, self._equity, self._equity)
