"""Provider contracts. Implementations live in sibling modules; no vendor types leak out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from autotrade.domain.models import MarketSnapshot, Pool, ProviderHealth, Token, WalletActivity


@dataclass(frozen=True)
class FetchResult[T]:
    """Validated items plus per-item rejections (invalid data is reported, never coerced)."""

    items: list[T] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    fetched: int = 0


@dataclass(frozen=True)
class MarketObservation:
    pool: Pool
    snapshot: MarketSnapshot


@runtime_checkable
class Provider(Protocol):
    name: str

    def health(self) -> ProviderHealth: ...


@runtime_checkable
class TokenMetadataProvider(Provider, Protocol):
    async def get_token(self, mint: str) -> FetchResult[Token]: ...


@runtime_checkable
class WalletActivityProvider(Provider, Protocol):
    async def get_activity(self, wallet: str, limit: int) -> FetchResult[WalletActivity]: ...


@runtime_checkable
class DexMarketDataProvider(Provider, Protocol):
    """Contract only in Stage 1: no DEX implementation ships until its API is verified."""

    async def get_market(self, mint: str) -> FetchResult[MarketObservation]: ...
