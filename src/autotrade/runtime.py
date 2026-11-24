"""Composition root: builds providers/jobs from config. Read-only; nothing here can trade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import AppConfig
from .data.providers.birdeye import BirdeyeProvider
from .data.providers.dexscreener import DexScreenerProvider
from .data.providers.http import ResilientHttp, RetryPolicy
from .data.providers.jupiter import JupiterProvider
from .data.providers.rpc_adapters import RpcTokenMetadataProvider, RpcWalletActivityProvider
from .data.providers.solana_rpc import SolanaRpcClient
from .data.storage.db import Database
from .domain.timeutil import Clock, utcnow
from .ingestion.jobs import (
    IngestJob,
    MarketSnapshotJob,
    OhlcvJob,
    TokenIntelJob,
    TokenMetadataJob,
    WalletActivityJob,
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


def build_jobs(config: AppConfig, prov: Providers, clock: Clock = utcnow) -> list[IngestJob]:
    """Jobs for the configured watchlists. Each provider is an independent job, so one provider
    being down never blocks the others (graceful degradation)."""
    d = config.data
    jobs: list[IngestJob] = []
    if d.watch_mints:
        jobs.append(TokenMetadataJob(RpcTokenMetadataProvider(prov.rpc), d.watch_mints))
        if prov.dex:
            jobs.append(MarketSnapshotJob(prov.dex, d.watch_mints, d.max_snapshot_age_seconds))
        if prov.jupiter:
            jobs.append(TokenIntelJob(prov.jupiter, d.watch_mints))
        if prov.birdeye:
            p = config.providers
            jobs.append(
                OhlcvJob(prov.birdeye, d.watch_mints, p.ohlcv_interval, p.ohlcv_candles, clock)
            )
    if d.watch_wallets:
        jobs.append(
            WalletActivityJob(
                RpcWalletActivityProvider(prov.rpc), d.watch_wallets, d.wallet_activity_limit, clock
            )
        )
    return jobs
