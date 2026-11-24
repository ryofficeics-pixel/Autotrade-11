"""Backtest assumptions. Costs are deliberately conservative; every value is explicit."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BacktestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    latency_seconds: int = Field(default=15, ge=0)  # decision -> earliest fill (next tick after)
    max_pending_seconds: int = Field(default=300, ge=1)  # no tick in time => missed fill
    max_hold_seconds: int = Field(default=3600, ge=60)
    slippage_buffer: Decimal = Field(default=Decimal("0.005"), ge=0, lt=1)  # extra, each side
    failed_tx_rate: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)  # deterministic draw
    dead_token_haircut: Decimal = Field(default=Decimal("1.0"), ge=0, le=1)  # 1.0 = total loss
    dead_after_seconds: int = Field(default=3600, ge=60)  # no tick this long before end => dead
    seed: str = "m5"
    max_data_gap_seconds: int = Field(default=900, ge=60)  # features/ticks across gaps are skipped
    apply_edge_gate: bool = False  # edge is what we measure; the live gate's hypothesis is skipped
