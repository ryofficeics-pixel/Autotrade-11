"""Resilient JSON-over-HTTP transport: explicit timeout, bounded retries, backoff, rate limiting."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from autotrade.domain.errors import (
    HttpStatusError,
    MalformedResponse,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransportError,
)
from autotrade.domain.timeutil import Clock, utcnow
from autotrade.observability.logging import redact

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    timeout_seconds: float = 10.0
    max_attempts: int = 3  # total attempts per call, including the first
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    max_requests_per_second: float = 4.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.timeout_seconds <= 0 or self.max_requests_per_second <= 0:
            raise ValueError("invalid retry policy")
        if self.backoff_base_seconds < 0 or self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("invalid backoff bounds")


class RateLimiter:
    """Spaces requests at least 1/rps apart (client-side politeness toward public endpoints)."""

    def __init__(self, rps: float, sleep: Sleep, now: Callable[[], float] = time.monotonic) -> None:
        self._interval = 1.0 / rps
        self._sleep = sleep
        self._now = now
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._now()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await self._sleep(wait)


@dataclass(frozen=True)
class HttpResult:
    data: Any
    requested_at: datetime
    received_at: datetime
    latency_ms: float


Parser = Callable[[Any], Any]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)  # seconds form only; HTTP-date form is ignored
    except ValueError:
        return None
    return value if value >= 0 else None


class ResilientHttp:
    def __init__(
        self,
        policy: RetryPolicy,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock = utcnow,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.policy = policy
        self._client = client or httpx.AsyncClient()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._limiter = limiter or RateLimiter(policy.max_requests_per_second, sleep)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backoff(self, attempt: int) -> float:
        p = self.policy
        delay = min(p.backoff_max_seconds, p.backoff_base_seconds * (2 ** (attempt - 1)))
        return float(delay * (0.5 + self._rng() / 2))  # jitter in [50%, 100%]

    async def _once(self, method: str, url: str, parse: Parser, **kw: Any) -> HttpResult:
        requested_at = self._clock()
        started = time.monotonic()
        try:
            response = await self._client.request(
                method, url, timeout=httpx.Timeout(self.policy.timeout_seconds), **kw
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"request timed out after {self.policy.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(redact(f"transport failure: {type(exc).__name__}: {exc}")) from exc
        received_at = self._clock()
        latency_ms = (time.monotonic() - started) * 1000
        if response.status_code == 429:
            raise RateLimited("HTTP 429 rate limited", retry_after=_retry_after(response))
        if response.status_code >= 400:
            body: object = None
            try:
                if len(response.content) <= 4096:
                    body = json.loads(response.content)
            except ValueError:
                pass
            raise HttpStatusError(response.status_code, f"HTTP {response.status_code}", body)
        try:
            data = json.loads(response.content, parse_float=Decimal)
        except ValueError as exc:
            raise MalformedResponse("response body is not valid JSON") from exc
        return HttpResult(parse(data), requested_at, received_at, latency_ms)

    async def post_json(self, url: str, payload: Any, parse: Parser = lambda d: d) -> HttpResult:
        return await self.request_json("POST", url, parse, json=payload)

    async def get_json(
        self,
        url: str,
        parse: Parser = lambda d: d,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResult:
        return await self.request_json("GET", url, parse, params=params, headers=headers)

    async def request_json(
        self, method: str, url: str, parse: Parser = lambda d: d, **kw: Any
    ) -> HttpResult:
        """POST with bounded retries. Raises the last ProviderError; never loops unbounded."""
        p = self.policy
        for attempt in range(1, p.max_attempts + 1):
            await self._limiter.acquire()
            try:
                return await self._once(method, url, parse, **kw)
            except ProviderError as err:
                if not err.retryable or attempt == p.max_attempts:
                    raise
                delay = self._backoff(attempt)
                if isinstance(err, RateLimited) and err.retry_after is not None:
                    if err.retry_after > p.backoff_max_seconds:
                        raise  # server asks for longer than our bound: surface, don't block
                    delay = max(delay, err.retry_after)
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover
