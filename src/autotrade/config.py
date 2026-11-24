"""Validated configuration with fail-safe defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, field_validator, model_validator

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
    max_requests_per_second: float = Field(default=4.0, gt=0, le=100)  # Solana RPC
    # DEX data sources (public, no key required except optional Birdeye)
    dexscreener_enabled: bool = True
    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_rps: float = Field(default=4.0, gt=0, le=5)  # documented 300 req/min
    jupiter_enabled: bool = True
    jupiter_base_url: str = "https://lite-api.jup.ag"
    jupiter_api_key: SecretStr | None = None  # optional; sent as x-api-key if set
    jupiter_rps: float = Field(default=1.0, gt=0, le=50)
    birdeye_api_key: SecretStr | None = None  # optional; Birdeye is disabled without it
    birdeye_base_url: str = "https://public-api.birdeye.so"
    birdeye_rps: float = Field(default=1.0, gt=0, le=50)
    ohlcv_interval: str = "15m"
    ohlcv_candles: int = Field(default=16, ge=1, le=1000)

    @field_validator("rpc_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("dexscreener_base_url", "jupiter_base_url", "birdeye_base_url")
    @classmethod
    def _base(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("ohlcv_interval")
    @classmethod
    def _interval(cls, v: str) -> str:
        if v not in {"1m", "5m", "15m", "30m", "1H", "4H", "1D"}:
            raise ValueError("ohlcv_interval must be one of 1m,5m,15m,30m,1H,4H,1D")
        return v

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
    price_tolerance_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, lt=1)
    wallet_activity_limit: int = Field(default=50, ge=1, le=1000)
    watch_mints: tuple[str, ...] = ()
    watch_wallets: tuple[str, ...] = ()
    discovery_enabled: bool = False  # opt-in: polls DEX Screener profiles for new mints
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


class SafetyPolicy(BaseModel):
    """Screening thresholds. Defaults are deliberately strict; relaxing them is a conscious,
    versioned decision. Unknown/missing data always rejects."""

    min_liquidity_usd: Decimal = Field(default=Decimal("10000"), ge=0)
    min_pool_age_minutes: int = Field(default=30, ge=0)  # confirmation delay after pair creation
    max_top_holders_pct: Decimal = Field(default=Decimal("40"), gt=0, le=100)  # Jupiter, percent
    min_sells_24h: int = Field(default=20, ge=1)  # sellability evidence (observed sells)
    min_sells_1h: int = Field(default=1, ge=1)
    min_sell_buy_ratio_24h: Decimal = Field(default=Decimal("0.10"), gt=0, le=10)
    allow_token_2022: bool = False  # extensions (fees, delegates) are not analysed in M2
    max_candidate_age_hours: int = Field(default=24, ge=1)
    max_candidates_per_cycle: int = Field(default=20, ge=1, le=100)


class SignalPolicy(BaseModel):
    """Momentum confirmation thresholds for strategy hybrid-v1. Research settings only."""

    min_ret_15m: Decimal = Field(default=Decimal("0.02"), gt=0)
    min_imbalance_5m: Decimal = Field(default=Decimal("0.05"), ge=-1, le=1)
    min_txn_rate_accel: Decimal = Field(default=Decimal("1.0"), gt=0)
    max_liq_drawdown_1h: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)
    ttl_seconds: int = Field(default=300, ge=10, le=3600)
    max_safety_age_seconds: int = Field(default=300, ge=10)
    invalidation_price_drop: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    invalidation_liquidity_drop: Decimal = Field(default=Decimal("0.20"), gt=0, lt=1)
    max_tx_parse_per_cycle: int = Field(default=10, ge=0, le=100)


class AppConfig(BaseModel):
    mode: str = "paper"
    live_enabled: bool = False
    initial_equity_idr: Annotated[Decimal, Field(gt=0)] = Decimal("1000000")
    risk: RiskLimits = Field(default_factory=RiskLimits)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    signals: SignalPolicy = Field(default_factory=SignalPolicy)

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
            ("dexscreener_base_url", "DEXSCREENER_BASE_URL"),
            ("dexscreener_rps", "DEXSCREENER_RPS"),
            ("jupiter_base_url", "JUPITER_BASE_URL"),
            ("jupiter_api_key", "JUPITER_API_KEY"),
            ("jupiter_rps", "JUPITER_RPS"),
            ("birdeye_api_key", "BIRDEYE_API_KEY"),
            ("birdeye_base_url", "BIRDEYE_BASE_URL"),
            ("birdeye_rps", "BIRDEYE_RPS"),
            ("ohlcv_interval", "AUTOTRADE_OHLCV_INTERVAL"),
            ("ohlcv_candles", "AUTOTRADE_OHLCV_CANDLES"),
        ]:
            put(providers, key, name)
        for key, name in [
            ("dexscreener_enabled", "AUTOTRADE_ENABLE_DEXSCREENER"),
            ("jupiter_enabled", "AUTOTRADE_ENABLE_JUPITER"),
        ]:
            enabled = flag(name)
            if enabled is not None:
                providers[key] = enabled

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
            ("price_tolerance_fraction", "AUTOTRADE_PRICE_TOLERANCE_FRACTION"),
            ("wallet_activity_limit", "AUTOTRADE_WALLET_ACTIVITY_LIMIT"),
            ("log_level", "AUTOTRADE_LOG_LEVEL"),
        ]:
            put(data, key, name)

        disc = flag("AUTOTRADE_DISCOVERY_ENABLED")
        if disc is not None:
            data["discovery_enabled"] = disc
        safety: dict[str, Any] = {}
        for key, name in [
            ("min_liquidity_usd", "AUTOTRADE_SAFETY_MIN_LIQUIDITY_USD"),
            ("min_pool_age_minutes", "AUTOTRADE_SAFETY_MIN_POOL_AGE_MINUTES"),
            ("max_top_holders_pct", "AUTOTRADE_SAFETY_MAX_TOP_HOLDERS_PCT"),
            ("min_sells_24h", "AUTOTRADE_SAFETY_MIN_SELLS_24H"),
            ("min_sells_1h", "AUTOTRADE_SAFETY_MIN_SELLS_1H"),
            ("min_sell_buy_ratio_24h", "AUTOTRADE_SAFETY_MIN_SELL_BUY_RATIO"),
            ("max_candidate_age_hours", "AUTOTRADE_CANDIDATE_MAX_AGE_HOURS"),
            ("max_candidates_per_cycle", "AUTOTRADE_MAX_CANDIDATES_PER_CYCLE"),
        ]:
            put(safety, key, name)
        allow22 = flag("AUTOTRADE_SAFETY_ALLOW_TOKEN_2022")
        if allow22 is not None:
            safety["allow_token_2022"] = allow22
        signals: dict[str, Any] = {}
        for key, name in [
            ("min_ret_15m", "AUTOTRADE_SIGNAL_MIN_RET_15M"),
            ("min_imbalance_5m", "AUTOTRADE_SIGNAL_MIN_IMBALANCE_5M"),
            ("min_txn_rate_accel", "AUTOTRADE_SIGNAL_MIN_TXN_ACCEL"),
            ("max_liq_drawdown_1h", "AUTOTRADE_SIGNAL_MAX_LIQ_DRAWDOWN_1H"),
            ("ttl_seconds", "AUTOTRADE_SIGNAL_TTL_SECONDS"),
            ("max_safety_age_seconds", "AUTOTRADE_SIGNAL_MAX_SAFETY_AGE_SECONDS"),
            ("invalidation_price_drop", "AUTOTRADE_SIGNAL_INVALIDATION_PRICE_DROP"),
            ("invalidation_liquidity_drop", "AUTOTRADE_SIGNAL_INVALIDATION_LIQ_DROP"),
            ("max_tx_parse_per_cycle", "AUTOTRADE_MAX_TX_PARSE_PER_CYCLE"),
        ]:
            put(signals, key, name)
        top: dict[str, Any] = {
            "providers": providers,
            "data": data,
            "safety": safety,
            "signals": signals,
        }
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
