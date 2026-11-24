from decimal import Decimal

import pytest
from pydantic import ValidationError

from autotrade.config import AppConfig, RiskLimits


def test_defaults_are_paper_and_live_locked():
    config = AppConfig()
    assert config.mode == "paper"
    assert config.live_enabled is False
    assert config.initial_equity_idr == Decimal("1000000")


def test_live_mode_is_rejected():
    with pytest.raises(ValidationError):
        AppConfig(mode="live", live_enabled=True)


def test_position_cap_cannot_exceed_total_exposure():
    with pytest.raises(ValidationError):
        RiskLimits(max_position_fraction=Decimal("0.5"), max_total_exposure_fraction=Decimal("0.3"))
