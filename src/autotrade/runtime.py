"""Composition root: builds providers/jobs from config. Read-only; nothing here can trade."""

from __future__ import annotations

import httpx

from .config import AppConfig
from .data.providers.http import ResilientHttp, RetryPolicy
from .data.providers.rpc_adapters import RpcTokenMetadataProvider, RpcWalletActivityProvider
from .data.providers.solana_rpc import SolanaRpcClient
from .data.storage.db import Database
from .domain.timeutil import Clock, utcnow
from .ingestion.jobs import IngestJob, TokenMetadataJob, WalletActivityJob


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


def build_jobs(config: AppConfig, rpc: SolanaRpcClient, clock: Clock = utcnow) -> list[IngestJob]:
    """Jobs for the configured watchlists. Market snapshots need a DEX provider, none shipped."""
    jobs: list[IngestJob] = []
    if config.data.watch_mints:
        jobs.append(TokenMetadataJob(RpcTokenMetadataProvider(rpc), config.data.watch_mints))
    if config.data.watch_wallets:
        jobs.append(
            WalletActivityJob(
                RpcWalletActivityProvider(rpc),
                config.data.watch_wallets,
                config.data.wallet_activity_limit,
                clock,
            )
        )
    return jobs
