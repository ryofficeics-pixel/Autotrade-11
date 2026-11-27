"""Composition root: builds providers/jobs from config. Read-only; nothing here can trade."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .config import AppConfig
from .data.providers.birdeye import BirdeyeProvider
from .data.providers.dexscreener import DexScreenerProvider
from .data.providers.http import ResilientHttp, RetryPolicy
from .data.providers.jupiter import JupiterProvider
from .data.providers.rpc_adapters import (
    RpcTokenMetadataProvider,
    RpcWalletActivityProvider,
    RpcWalletFlowProvider,
)
from .data.providers.solana_rpc import SolanaRpcClient
from .data.storage.db import Database
from .domain.timeutil import Clock, utcnow
from .ingestion.jobs import (
    DiscoveryJob,
    IngestJob,
    MarketSnapshotJob,
    OhlcvJob,
    SellQuoteJob,
    TokenIntelJob,
    TokenMetadataJob,
    WalletActivityJob,
    WalletFlowJob,
)


def build_database(config: AppConfig, clock: Clock = utcnow) -> Database:
    return Database(config.data.db_path, clock=clock)


def build_rpc(
    config: AppConfig, *, clock: Clock = utcnow, client: httpx.AsyncClient | None = None
) -> SolanaRpcClient:
    p = config.providers
    policy = RetryPolicy(
        timeout_seconds=p.timeout_seconds,
        max_attempts=p.max_attempts,
        backoff_base_seconds=p.backoff_base_seconds,
        backoff_max_seconds=p.backoff_max_seconds,
        max_requests_per_second=p.max_requests_per_second,
    )
    http = ResilientHttp(policy, client=client, clock=clock)
    return SolanaRpcClient(p.endpoints, http, commitment=p.commitment, clock=clock)


@dataclass
class Providers:
    """Everything the ingest command talks to. Disabled providers are None."""

    rpc: SolanaRpcClient
    dex: DexScreenerProvider | None = None
    jupiter: JupiterProvider | None = None
    birdeye: BirdeyeProvider | None = None

    async def aclose(self) -> None:
        for p in (self.rpc, self.dex, self.jupiter, self.birdeye):
            if p is not None:
                await p.aclose()

    def all(self) -> list[Any]:
        return [p for p in (self.rpc, self.dex, self.jupiter, self.birdeye) if p is not None]


def _http(config: AppConfig, rps: float, clock: Clock) -> ResilientHttp:
    p = config.providers
    policy = RetryPolicy(
        timeout_seconds=p.timeout_seconds,
        max_attempts=p.max_attempts,
        backoff_base_seconds=p.backoff_base_seconds,
        backoff_max_seconds=p.backoff_max_seconds,
        max_requests_per_second=rps,
    )
    return ResilientHttp(policy, clock=clock)


def build_providers(config: AppConfig, clock: Clock = utcnow) -> Providers:
    p = config.providers
    out = Providers(rpc=build_rpc(config, clock=clock))
    if p.dexscreener_enabled:
        out.dex = DexScreenerProvider(
            "dexscreener", p.dexscreener_base_url, _http(config, p.dexscreener_rps, clock), clock
        )
    if p.jupiter_enabled:
        headers = {"x-api-key": p.jupiter_api_key.get_secret_value()} if p.jupiter_api_key else {}
        out.jupiter = JupiterProvider(
            "jupiter", p.jupiter_base_url, _http(config, p.jupiter_rps, clock), clock, headers
        )
    if p.birdeye_api_key:  # optional: no key, no Birdeye
        out.birdeye = BirdeyeProvider(
            "birdeye",
            p.birdeye_base_url,
            _http(config, p.birdeye_rps, clock),
            clock,
            {"X-API-KEY": p.birdeye_api_key.get_secret_value(), "x-chain": "solana"},
        )
    return out


def build_jobs(
    config: AppConfig,
    prov: Providers,
    clock: Clock = utcnow,
    mints: Sequence[str] | None = None,
    pending_flows: Callable[[], Sequence[tuple[str, str]]] | None = None,
    sell_probes: Callable[[], Sequence[tuple[str, int]]] | None = None,
) -> list[IngestJob]:
    """Jobs for the configured watchlists. Each provider is an independent job, so one provider
    being down never blocks the others (graceful degradation)."""
    d = config.data
    mints = tuple(d.watch_mints if mints is None else mints)
    jobs: list[IngestJob] = []
    if mints:
        jobs.append(TokenMetadataJob(RpcTokenMetadataProvider(prov.rpc), mints))
        if prov.dex:
            jobs.append(MarketSnapshotJob(prov.dex, mints, d.max_snapshot_age_seconds))
        if prov.jupiter:
            jobs.append(TokenIntelJob(prov.jupiter, mints))
        if prov.jupiter and sell_probes is not None:
            jobs.append(SellQuoteJob(prov.jupiter, sell_probes))
        if prov.birdeye:
            p = config.providers
            jobs.append(OhlcvJob(prov.birdeye, mints, p.ohlcv_interval, p.ohlcv_candles, clock))
    if d.watch_wallets:
        jobs.append(
            WalletActivityJob(
                RpcWalletActivityProvider(prov.rpc), d.watch_wallets, d.wallet_activity_limit, clock
            )
        )
    if pending_flows is not None and d.watch_wallets:
        jobs.append(WalletFlowJob(RpcWalletFlowProvider(prov.rpc), pending_flows, clock))
    return jobs


def build_discovery_job(config: AppConfig, prov: Providers) -> DiscoveryJob | None:
    """Opt-in (AUTOTRADE_DISCOVERY_ENABLED). Runs before each cycle so new candidates are
    ingested and screened in the same cycle."""
    if config.data.discovery_enabled and prov.dex:
        return DiscoveryJob(prov.dex, config.safety.max_candidates_per_cycle)
    return None
