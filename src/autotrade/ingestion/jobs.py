"""Ingestion jobs: fetch (bounded concurrency) -> dedupe -> classify -> persist."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from autotrade.data.providers.base import (
    DexMarketDataProvider,
    DiscoveryProvider,
    FetchResult,
    MarketObservation,
    OhlcvProvider,
    Provider,
    TokenIntelProvider,
    TokenMetadataProvider,
    WalletActivityProvider,
)
from autotrade.data.providers.birdeye import INTERVAL_SECONDS
from autotrade.data.storage import repositories as repo
from autotrade.domain.errors import ProviderError
from autotrade.domain.models import DataQuality, Wallet
from autotrade.domain.timeutil import Clock


@dataclass
class Batch:
    items: list[Any] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    errors: list[ProviderError] = field(default_factory=list)
    targets: int = 0
    fetched: int = 0
    stale: int = 0


class IngestJob(Protocol):
    name: str

    @property
    def provider(self) -> Provider: ...

    async def fetch(self, sem: asyncio.Semaphore) -> Batch: ...

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts: ...


async def _gather(
    targets: Sequence[str],
    fetch_one: Callable[[str], Awaitable[FetchResult[Any]]],
    sem: asyncio.Semaphore,
) -> Batch:
    batch = Batch(targets=len(targets))

    async def one(target: str) -> None:
        async with sem:
            try:
                result = await fetch_one(target)
            except ProviderError as err:
                batch.errors.append(err)
                return
        batch.items.extend(result.items)
        batch.rejections.extend(result.rejections)
        batch.fetched += result.fetched

    await asyncio.gather(*(one(t) for t in targets))
    return batch


def dedupe(batch: Batch, key: Callable[[Any], Hashable], same: Callable[[Any, Any], bool]) -> None:
    """Collapse exact repeats; reject every member of a key whose copies contradict."""
    groups: dict[Hashable, list[Any]] = {}
    for item in batch.items:
        groups.setdefault(key(item), []).append(item)
    kept: list[Any] = []
    for k, members in groups.items():
        if all(same(members[0], m) for m in members[1:]):
            kept.append(members[0])
        else:
            batch.rejections.append(f"contradictory duplicate observations for {k!r}")
    batch.items = kept


def _outcomes(items: Sequence[Any], fn: Callable[[Any], repo.Outcome]) -> repo.Counts:
    counts = repo.Counts()
    for item in items:
        counts.add(fn(item))
    return counts


class TokenMetadataJob:
    name = "token_metadata"

    def __init__(self, provider: TokenMetadataProvider, mints: Sequence[str]) -> None:
        self.provider, self._mints = provider, list(mints)

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        batch = await _gather(self._mints, self.provider.get_token, sem)
        dedupe(
            batch,
            lambda t: t.mint,
            lambda a, b: (
                a.model_dump(exclude={"observed_at", "received_at"})
                == b.model_dump(exclude={"observed_at", "received_at"})
            ),
        )
        return batch

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        return _outcomes(batch.items, lambda t: repo.upsert_token(conn, t))


class WalletActivityJob:
    name = "wallet_activity"

    def __init__(
        self, provider: WalletActivityProvider, wallets: Sequence[str], limit: int, clock: Clock
    ) -> None:
        self.provider, self._wallets, self._limit, self._clock = (
            provider,
            list(wallets),
            limit,
            clock,
        )

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        batch = await _gather(
            self._wallets, lambda w: self.provider.get_activity(w, self._limit), sem
        )
        dedupe(
            batch,
            lambda a: (a.wallet_address, a.signature),
            lambda a, b: (a.slot, a.succeeded) == (b.slot, b.succeeded),
        )
        return batch

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        now = self._clock()
        for address in sorted({a.wallet_address for a in batch.items}):
            repo.upsert_wallet(
                conn,
                Wallet(
                    source=self.provider.name, observed_at=now, received_at=now, address=address
                ),
            )
        return _outcomes(batch.items, lambda a: repo.insert_wallet_activity(conn, a))


class MarketSnapshotJob:
    name = "market_snapshots"

    def __init__(
        self, provider: DexMarketDataProvider, mints: Sequence[str], max_age_seconds: float
    ) -> None:
        self.provider, self._mints, self._max_age = provider, list(mints), max_age_seconds

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        batch = await _gather(self._mints, self.provider.get_market, sem)
        dedupe(
            batch,
            lambda o: (o.snapshot.source, o.snapshot.pool_address, o.snapshot.observed_at),
            lambda a, b: a.snapshot.price_quote == b.snapshot.price_quote,
        )
        batch.items = [self._classify(o, batch) for o in batch.items]
        return batch

    def _classify(self, obs: MarketObservation, batch: Batch) -> MarketObservation:
        snap = obs.snapshot
        age = (snap.received_at - snap.observed_at).total_seconds()
        if age <= self._max_age:
            return obs
        batch.stale += 1
        note = f"observation {age:.0f}s old exceeds {self._max_age:.0f}s freshness limit"
        flagged = snap.model_copy(
            update={"quality": DataQuality.STALE, "quality_notes": (*snap.quality_notes, note)}
        )
        return MarketObservation(obs.pool, flagged)

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        counts = repo.Counts()
        for obs in batch.items:
            pool_outcome = repo.upsert_pool(conn, obs.pool)
            if pool_outcome is repo.Outcome.CONFLICT:
                counts.add(pool_outcome)
                continue
            counts.add(repo.insert_snapshot(conn, obs.snapshot))
        return counts


class TokenIntelJob:
    name = "token_intel"

    def __init__(self, provider: TokenIntelProvider, mints: Sequence[str]) -> None:
        self.provider, self._mints = provider, list(mints)

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        batch = await _gather(self._mints, self.provider.get_intel, sem)
        dedupe(batch, lambda t: (t.source, t.mint, t.observed_at), lambda a, b: a == b)
        return batch

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        return _outcomes(batch.items, lambda t: repo.insert_intel(conn, t))


class OhlcvJob:
    name = "ohlcv"

    def __init__(
        self,
        provider: OhlcvProvider,
        mints: Sequence[str],
        interval: str,
        candles: int,
        clock: Clock,
    ) -> None:
        self.provider, self._mints, self._interval = provider, list(mints), interval
        self._candles, self._clock = candles, clock

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        end = self._clock()
        start = end - timedelta(seconds=INTERVAL_SECONDS[self._interval] * self._candles)
        batch = await _gather(
            self._mints, lambda m: self.provider.get_candles(m, self._interval, start, end), sem
        )
        dedupe(batch, lambda c: (c.source, c.mint, c.interval, c.open_time), lambda a, b: a == b)
        return batch

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        return _outcomes(batch.items, lambda c: repo.insert_candle(conn, c))


class DiscoveryJob:
    """Records newly seen mints as candidates. A candidate is only a name on a list:
    it is not evaluated as safe and not a buy signal."""

    name = "discovery"

    def __init__(self, provider: DiscoveryProvider, max_new: int) -> None:
        self.provider, self._max_new = provider, max_new

    async def fetch(self, sem: asyncio.Semaphore) -> Batch:
        batch = Batch(targets=1)
        async with sem:
            try:
                result = await self.provider.discover()
            except ProviderError as err:
                batch.errors.append(err)
                return batch
        batch.items, batch.rejections = list(result.items), list(result.rejections)
        batch.fetched = result.fetched
        dedupe(batch, lambda d: d.mint, lambda a, b: a.feed == b.feed)
        batch.items = batch.items[: self._max_new]
        return batch

    def persist(self, conn: sqlite3.Connection, batch: Batch) -> repo.Counts:
        return _outcomes(batch.items, lambda d: repo.upsert_candidate(conn, d))
