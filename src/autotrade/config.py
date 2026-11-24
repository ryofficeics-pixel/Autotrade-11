"""Validated configuration with fail-safe defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from .domain.addresses import check_pubkey

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"  # public, rate-limited, no SLA
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


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
    def validate_exposure(self) -> RiskLimits:
        if self.max_position_fraction > self.max_total_exposure_fraction:
            raise ValueError("single-position cap cannot exceed aggregate exposure cap")
        return self


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("provider URL must be http(s) with a host")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise ValueError("plain http is only allowed for localhost")
    return url


class ProviderSettings(BaseModel):
    """Public endpoints by default; no API key is required. A keyed RPC URL may be supplied via
    SOLANA_RPC_URL; it is never logged or stored (only the host is)."""

    rpc_url: str = DEFAULT_RPC_URL
    rpc_fallback_urls: tuple[str, ...] = ()
    commitment: str = "confirmed"
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_base_seconds: float = Field(default=0.5, ge=0, le=60)
    backoff_max_seconds: float = Field(default=8.0, ge=0, le=300)
    max_requests_per_second: float = Field(default=4.0, gt=0, le=100)

    @field_validator("rpc_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("rpc_fallback_urls")
    @classmethod
    def _urls(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_check_url(u) for u in v)

    @field_validator("commitment")
    @classmethod
    def _commitment(cls, v: str) -> str:
        if v not in {"processed", "confirmed", "finalized"}:
            raise ValueError("commitment must be processed|confirmed|finalized")
        return v

    @model_validator(mode="after")
    def _backoff(self) -> ProviderSettings:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self

    @property
    def endpoints(self) -> list[str]:
        return [self.rpc_url, *self.rpc_fallback_urls]


class DataSettings(BaseModel):
    db_path: Path = Path("data/autotrade.sqlite3")
    offline: bool = False
    max_concurrency: int = Field(default=4, ge=1, le=16)
    poll_interval_seconds: float = Field(default=60.0, ge=5)
    max_snapshot_age_seconds: float = Field(default=30.0, gt=0)
    wallet_activity_limit: int = Field(default=50, ge=1, le=1000)
    watch_mints: tuple[str, ...] = ()
    watch_wallets: tuple[str, ...] = ()
    log_level: str = "INFO"

    @field_validator("watch_mints", "watch_wallets")
    @classmethod
    def _addresses(cls, v: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        return tuple(check_pubkey(a) for a in v)

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG|INFO|WARNING|ERROR")
        return v


class AppConfig(BaseModel):
    mode: str = "paper"
    live_enabled: bool = False
    initial_equity_idr: Annotated[Decimal, Field(gt=0)] = Decimal("1000000")
    risk: RiskLimits = Field(default_factory=RiskLimits)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    data: DataSettings = Field(default_factory=DataSettings)

    @model_validator(mode="after")
    def fail_closed(self) -> AppConfig:
        if self.mode != "paper" or self.live_enabled:
            raise ValueError("initial release supports PAPER mode only; live execution is locked")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        """Build config from environment variables. Unset/empty values use safe defaults;
        malformed values raise (pydantic ValidationError / ValueError) instead of being ignored."""
        e = dict(os.environ if env is None else env)

        def get(name: str) -> str | None:
            v = e.get(name)
            return v.strip() if v and v.strip() else None

        def csv(name: str) -> tuple[str, ...]:
            raw = get(name)
            return tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else ()

        def flag(name: str) -> bool | None:
            raw = get(name)
            if raw is None:
                return None
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"{name} must be a boolean, got {raw!r}")

        def put(target: dict[str, Any], key: str, name: str, value: str | None = None) -> None:
            value = get(name) if value is None else value
            if value is not None:
                target[key] = value

        providers: dict[str, Any] = {}
        put(providers, "rpc_url", "SOLANA_RPC_URL")
        if csv("SOLANA_RPC_FALLBACK_URLS"):
            providers["rpc_fallback_urls"] = csv("SOLANA_RPC_FALLBACK_URLS")
        for key, name in [
            ("commitment", "SOLANA_RPC_COMMITMENT"),
            ("timeout_seconds", "AUTOTRADE_HTTP_TIMEOUT_SECONDS"),
            ("max_attempts", "AUTOTRADE_MAX_ATTEMPTS"),
            ("backoff_base_seconds", "AUTOTRADE_BACKOFF_BASE_SECONDS"),
            ("backoff_max_seconds", "AUTOTRADE_BACKOFF_MAX_SECONDS"),
            ("max_requests_per_second", "AUTOTRADE_MAX_REQUESTS_PER_SECOND"),
        ]:
            put(providers, key, name)

        data: dict[str, Any] = {
            "watch_mints": csv("AUTOTRADE_WATCH_MINTS"),
            "watch_wallets": csv("AUTOTRADE_WATCH_WALLETS"),
        }
        offline = flag("AUTOTRADE_OFFLINE")
        if offline is not None:
            data["offline"] = offline
        for key, name in [
            ("db_path", "AUTOTRADE_DB_PATH"),
            ("max_concurrency", "AUTOTRADE_MAX_CONCURRENCY"),
            ("poll_interval_seconds", "AUTOTRADE_POLL_INTERVAL_SECONDS"),
            ("max_snapshot_age_seconds", "AUTOTRADE_MAX_SNAPSHOT_AGE_SECONDS"),
            ("wallet_activity_limit", "AUTOTRADE_WALLET_ACTIVITY_LIMIT"),
            ("log_level", "AUTOTRADE_LOG_LEVEL"),
        ]:
            put(data, key, name)

        top: dict[str, Any] = {"providers": providers, "data": data}
        put(top, "mode", "AUTOTRADE_MODE")
        put(top, "initial_equity_idr", "AUTOTRADE_INITIAL_EQUITY_IDR")
        live = flag("AUTOTRADE_LIVE_ENABLED")
        if live is not None:
            top["live_enabled"] = live
        return cls.model_validate(top)


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> int:
    """Minimal KEY=VALUE loader. Never overrides variables already set. Returns count loaded."""
    target = os.environ if environ is None else environ
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in target:
            target[key] = value
            loaded += 1
    return loaded
