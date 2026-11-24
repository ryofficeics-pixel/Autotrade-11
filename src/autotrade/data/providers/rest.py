"""Shared base for REST providers: GET with health tracking and shape helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from autotrade.domain.errors import MalformedResponse, ProviderError
from autotrade.domain.models import ProviderHealth
from autotrade.domain.timeutil import Clock

from .health import HealthTracker
from .http import HttpResult, ResilientHttp


class RestProvider:
    name: str

    def __init__(
        self,
        name: str,
        base_url: str,
        http: ResilientHttp,
        clock: Clock,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._http = http
        self._clock = clock
        self._headers = dict(headers or {})
        self._tracker = HealthTracker(name, urlsplit(base_url).hostname, clock)

    def health(self) -> ProviderHealth:
        return self._tracker.snapshot()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> HttpResult:
        try:
            result = await self._http.get_json(
                f"{self._base}{path}", params=params, headers=self._headers
            )
        except ProviderError as err:
            self._tracker.record_failure(err)
            raise
        self._tracker.record_success(result.latency_ms)
        return result

    def _fail(self, err: ProviderError) -> None:
        """Record a response that arrived but violated the documented contract."""
        self._tracker.record_failure(err)


def expect(data: Any, kind: type, what: str) -> Any:
    if not isinstance(data, kind):
        raise MalformedResponse(f"{what}: expected {kind.__name__}, got {type(data).__name__}")
    return data
