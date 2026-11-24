from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from autotrade.domain.addresses import is_valid_pubkey, is_valid_signature
from autotrade.domain.models import DataQuality, MarketSnapshot, Pool, Token, WalletActivity
from autotrade.domain.timeutil import fmt_ts, from_unix, parse_ts, to_utc
from tests.conftest import MINT, MINT2, T0, WALLET, sig


def token(**kw):
    base = dict(
        source="t", observed_at=T0, received_at=T0, mint=MINT, decimals=9, authorities_known=True
    )
    return Token(**{**base, **kw})


def snap(**kw):
    base = dict(
        source="t",
        observed_at=T0,
        received_at=T0,
        pool_address=WALLET,
        base_mint=MINT,
        quote_mint=MINT2,
        price_quote="1.5",
    )
    return MarketSnapshot(**{**base, **kw})


def test_token_valid_and_frozen():
    t = token()
    with pytest.raises(ValidationError):
        t.decimals = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [{"mint": "nope"}, {"decimals": 256}, {"decimals": -1}, {"decimals": 6.0}, {"supply_raw": -5}],
)
def test_token_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        token(**bad)


def test_unknown_authority_cannot_carry_value():
    with pytest.raises(ValidationError):
        token(authorities_known=False, mint_authority=WALLET)


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        token(observed_at=datetime(2026, 1, 1), received_at=T0)


def test_timestamps_normalized_to_utc():
    jakarta = timezone(timedelta(hours=7))
    t = token(observed_at=datetime(2026, 1, 1, 19, 0, tzinfo=jakarta))
    assert t.observed_at == T0 and t.observed_at.tzinfo is UTC
    assert token(observed_at="2026-01-01T12:00:00Z").observed_at == T0


def test_future_observation_rejected():
    with pytest.raises(ValidationError):
        token(observed_at=T0 + timedelta(minutes=5))


def test_non_ok_quality_requires_notes():
    with pytest.raises(ValidationError):
        token(quality=DataQuality.STALE)
    assert token(quality=DataQuality.STALE, quality_notes=("old",)).quality is DataQuality.STALE


def test_decimal_exactness_and_float_rejection():
    assert snap(price_quote="0.1").price_quote + Decimal("0.2") == Decimal("0.3")
    for bad in (0.1, True, "abc", "NaN", "Infinity", "0", "-1"):
        with pytest.raises(ValidationError):
            snap(price_quote=bad)
    assert snap(liquidity_usd=None).liquidity_usd is None
    with pytest.raises(ValidationError):
        snap(liquidity_usd="-1")


def test_pool_requires_distinct_mints():
    with pytest.raises(ValidationError):
        Pool(
            source="t",
            observed_at=T0,
            received_at=T0,
            address=WALLET,
            dex="x",
            base_mint=MINT,
            quote_mint=MINT,
        )


def test_wallet_activity_validation():
    ok = WalletActivity(
        source="t",
        observed_at=T0,
        received_at=T0,
        wallet_address=WALLET,
        signature=sig(1),
        slot=5,
        succeeded=True,
    )
    assert ok.kind == "signature"
    with pytest.raises(ValidationError):
        WalletActivity(
            source="t",
            observed_at=T0,
            received_at=T0,
            wallet_address=WALLET,
            signature="short",
            slot=5,
            succeeded=True,
        )


def test_timeutil_roundtrip_and_errors():
    assert parse_ts(fmt_ts(T0)) == T0
    assert fmt_ts(T0) == "2026-01-01T12:00:00.000000Z"
    assert from_unix(0) == datetime(1970, 1, 1, tzinfo=UTC)
    for bad in (True, "x", 10**20):
        with pytest.raises(ValueError):
            from_unix(bad) if not isinstance(bad, str) else to_utc(bad)
    with pytest.raises(ValueError):
        to_utc(1234)


def test_address_validation():
    assert is_valid_pubkey(MINT) and is_valid_pubkey("1" * 32)
    assert not is_valid_pubkey("0OIl" * 9) and not is_valid_pubkey("abc")
    assert is_valid_signature(sig(3)) and not is_valid_signature(MINT)
