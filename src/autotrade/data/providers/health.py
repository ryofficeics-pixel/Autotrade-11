"""Per-provider health tracking driven by real call outcomes."""

from __future__ import annotations

from datetime import datetime

from autotrade.domain.errors import ErrorKind, ProviderError
from autotrade.domain.models import HealthStatus, ProviderHealth
from autotrade.domain.timeutil import Clock
from autotrade.observability.logging import redact

DOWN_AFTER_FAILURES = 3


class HealthTracker:
    def __init__(self, provider: str, endpoint: str | None, clock: Clock) -> None:
        self._provider = provider
        self._endpoint = endpoint
        self._clock = clock
        self._failures = 0
        self._last_success: datetime | None = None
        self._last_latency: float | None = None
        self._last_error: ProviderError | None = None
        self._touched = False

    def record_success(self, latency_ms: float) -> None:
        self._touched = True
        self._failures = 0
        self._last_success = self._clock()
        self._last_latency = latency_ms
        self._last_error = None

    def record_failure(self, error: ProviderError) -> None:
        self._touched = True
        self._failures += 1
        self._last_error = error

    def snapshot(self) -> ProviderHealth:
        if not self._touched:
            status = HealthStatus.UNKNOWN
        elif self._failures == 0:
            status = HealthStatus.HEALTHY
        elif self._failures >= DOWN_AFTER_FAILURES:
            status = HealthStatus.DOWN
        else:
            status = HealthStatus.DEGRADED
        error = self._last_error
        if (
            error is not None
            and error.kind is ErrorKind.RATE_LIMITED
            and status is HealthStatus.DOWN
        ):
            status = HealthStatus.DEGRADED  # throttled, not dead
        return ProviderHealth(
            provider=self._provider,
            endpoint=self._endpoint,
            status=status,
            checked_at=self._clock(),
            latency_ms=self._last_latency,
            consecutive_failures=self._failures,
            last_success_at=self._last_success,
            last_error_kind=error.kind.value if error else None,
            last_error_message=redact(error.message)[:300] if error else None,
        )
