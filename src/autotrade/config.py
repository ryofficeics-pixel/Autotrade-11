"""Validated configuration with fail-safe defaults."""
from decimal import Decimal
from pydantic import BaseModel, Field, model_validator


class RiskLimits(BaseModel):
    risk_per_trade_fraction: Decimal = Field(default=Decimal("0.005"), gt=0, le=1)
    max_position_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_total_exposure_fraction: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    reserve_cash_fraction: Decimal = Field(default=Decimal("0.70"), ge=0, lt=1)
    daily_loss_stop_fraction: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    max_concurrent_positions: int = Field(default=3, ge=1)
    max_slippage_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    max_quote_age_seconds: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validate_exposure(self):
        if self.max_position_fraction > self.max_total_exposure_fraction:
            raise ValueError("single-position cap cannot exceed aggregate exposure cap")
        return self


class AppConfig(BaseModel):
    mode: str = "paper"
    live_enabled: bool = False
    initial_equity_idr: Decimal = Field(default=Decimal("1000000"), gt=0)
    risk: RiskLimits = Field(default_factory=RiskLimits)

    @model_validator(mode="after")
    def fail_closed(self):
        if self.mode != "paper" or self.live_enabled:
            raise ValueError("initial release supports PAPER mode only; live execution is locked")
        return self
