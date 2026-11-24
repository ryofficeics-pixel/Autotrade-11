"""Normalized, validated internal records. Invalid input raises; it is never coerced."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .addresses import check_pubkey, check_signature
from .timeutil import to_utc

MAX_CLOCK_SKEW = timedelta(seconds=60)


class DataQuality(StrEnum):
    OK = "ok"
    STALE = "stale"
    PARTIAL = "partial"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


def _strict_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("floats/bools rejected; pass Decimal, int or numeric string")
    try:
        if isinstance(value, Decimal | int):
            result = Decimal(value)
        elif isinstance(value, str):
            result = Decimal(value.strip())
        else:
            raise ValueError("unsupported decimal input type")
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("non-finite decimal rejected")
    return result


Amount = Annotated[Decimal, BeforeValidator(_strict_decimal)]
UtcDatetime = Annotated[datetime, BeforeValidator(to_utc)]
Address = Annotated[str, AfterValidator(check_pubkey)]
Signature = Annotated[str, AfterValidator(check_signature)]
Count = Annotated[int, Field(ge=0, strict=True)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Provenanced(_Model):
    source: Text
    observed_at: UtcDatetime
    received_at: UtcDatetime
    quality: DataQuality = DataQuality.OK
    quality_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_provenance(self) -> Self:
        if self.observed_at > self.received_at + MAX_CLOCK_SKEW:
            raise ValueError("observed_at is in the future relative to received_at")
        if self.quality is not DataQuality.OK and not self.quality_notes:
            raise ValueError("non-OK quality requires quality_notes")
        return self


class Token(_Provenanced):
    mint: Address
    decimals: Annotated[int, Field(ge=0, le=255, strict=True)]
    symbol: Text | None = None
    name: Text | None = None
    token_program: Address | None = None
    # When authorities_known is False, a None authority means UNKNOWN, not revoked.
    authorities_known: bool
    mint_authority: Address | None = None
    freeze_authority: Address | None = None
    supply_raw: Annotated[int, Field(ge=0, strict=True)] | None = None

    @model_validator(mode="after")
    def _authorities(self) -> Self:
        if not self.authorities_known and (self.mint_authority or self.freeze_authority):
            raise ValueError("authority set while authorities_known is False")
        return self


class Pool(_Provenanced):
    address: Address
    dex: Text
    base_mint: Address
    quote_mint: Address
    program_id: Address | None = None
    created_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.base_mint == self.quote_mint:
            raise ValueError("base_mint and quote_mint must differ")
        return self


class MarketSnapshot(_Provenanced):
    pool_address: Address
    base_mint: Address
    quote_mint: Address
    price_quote: Amount = Field(gt=0)
    price_usd: Amount | None = Field(default=None, gt=0)
    liquidity_usd: Amount | None = Field(default=None, ge=0)
    volume_24h_usd: Amount | None = Field(default=None, ge=0)
    fdv_usd: Amount | None = Field(default=None, ge=0)
    market_cap_usd: Amount | None = Field(default=None, ge=0)
    buys_5m: Count | None = None
    sells_5m: Count | None = None
    buys_1h: Count | None = None
    sells_1h: Count | None = None
    buys_24h: Count | None = None
    sells_24h: Count | None = None
    pair_created_at: UtcDatetime | None = None


class Wallet(_Provenanced):
    address: Address
    label: Text | None = None


class WalletActivity(_Provenanced):
    wallet_address: Address
    signature: Signature
    slot: Annotated[int, Field(ge=0, strict=True)]
    block_time: UtcDatetime | None = None
    succeeded: bool
    kind: Text = "signature"


class ProviderHealth(_Model):
    provider: Text
    endpoint: Text | None = None  # host only; never a full URL
    status: HealthStatus
    checked_at: UtcDatetime
    latency_ms: Annotated[float, Field(ge=0)] | None = None
    consecutive_failures: Annotated[int, Field(ge=0)] = 0
    last_success_at: UtcDatetime | None = None
    last_error_kind: str | None = None
    last_error_message: Annotated[str, StringConstraints(max_length=300)] | None = None


class IngestionResult(_Model):
    job: Text
    provider: Text
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime
    fetched: int = 0
    accepted: int = 0
    rejected: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    stale: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    rejections: tuple[str, ...] = ()
    run_id: int | None = None


class TokenIntel(_Provenanced):
    """PROVIDER-REPORTED token facts (e.g. Jupiter). Never treated as on-chain verified;
    compare against `tokens` (RPC) via cross-checks."""

    mint: Address
    symbol: Text | None = None
    name: Text | None = None
    decimals: Annotated[int, Field(ge=0, le=255, strict=True)] | None = None
    is_verified: bool | None = None
    tags: tuple[str, ...] = ()
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None
    top_holders_pct: Amount | None = Field(default=None, ge=0)
    holder_count: Count | None = None
    organic_score: Amount | None = None
    usd_price: Amount | None = Field(default=None, gt=0)
    liquidity_usd: Amount | None = Field(default=None, ge=0)
    market_cap_usd: Amount | None = Field(default=None, ge=0)
    fdv_usd: Amount | None = Field(default=None, ge=0)


class Candle(_Provenanced):
    mint: Address
    interval: Text
    open_time: UtcDatetime
    open: Amount = Field(gt=0)
    high: Amount = Field(gt=0)
    low: Amount = Field(gt=0)
    close: Amount = Field(gt=0)
    volume: Amount = Field(ge=0)

    @model_validator(mode="after")
    def _ohlc(self) -> Self:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("inconsistent OHLC values")
        return self


class CheckStatus(StrEnum):
    OK = "ok"
    DISCREPANCY = "discrepancy"
    UNAVAILABLE = "unavailable"  # an input was missing/stale: result is UNKNOWN, not "ok"


class CrossCheck(_Model):
    check_type: Text  # price_usd | decimals | mint_authority | freeze_authority
    mint: Address
    status: CheckStatus
    checked_at: UtcDatetime
    source_a: Text | None = None
    value_a: str | None = None
    source_b: Text | None = None
    value_b: str | None = None
    deviation: Amount | None = None
    detail: Text | None = None


class DiscoveredToken(_Provenanced):
    """A mint seen in a discovery feed. Discovery is NOT a buy signal and not a safety verdict."""

    mint: Address
    feed: Text


class RuleStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"  # missing/stale/unverifiable critical data; treated as a rejection


class Verdict(StrEnum):
    PASS = "pass"  # all safety rules passed; this is a screening result, never a trade signal
    REJECT = "reject"


class RuleResult(_Model):
    rule: Text
    status: RuleStatus
    observed: str | None = None
    threshold: str | None = None
    detail: str | None = None


class SafetyEvaluation(_Model):
    mint: Address
    ruleset_version: Text
    evaluated_at: UtcDatetime
    verdict: Verdict
    rules: tuple[RuleResult, ...]
    inputs: dict[str, object] = Field(default_factory=dict)  # row ids/timestamps used

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{r.rule}:{r.status.value}" for r in self.rules if r.status is not RuleStatus.PASS
        )
