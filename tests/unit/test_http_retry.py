import asyncio

import httpx
import pytest

from autotrade.data.providers.http import RateLimiter, ResilientHttp, RetryPolicy
from autotrade.domain.errors import (
    HttpStatusError,
    MalformedResponse,
    ProviderTimeout,
    RateLimited,
    TransportError,
)
from tests.conftest import Sleeper, _free_limiter


def make(handler, clock, sleeper, attempts=3):
    return ResilientHttp(
        RetryPolicy(
            timeout_seconds=1,
            max_attempts=attempts,
            backoff_base_seconds=1,
            backoff_max_seconds=8,
            max_requests_per_second=1000,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleep=sleeper,
        rng=lambda: 1.0,
        limiter=_free_limiter(),
    )


def run(coro):
    return asyncio.run(coro)


def test_success_records_timestamps(clock, sleeper):
    http = make(lambda r: httpx.Response(200, json={"a": 1}), clock, sleeper)
    res = run(http.post_json("https://x.test", {}))
    assert res.data == {"a": 1} and res.requested_at == clock.now and res.latency_ms >= 0


def test_retries_5xx_with_exponential_backoff_then_succeeds(clock, sleeper):
    seq = iter([503, 502, 200])
    http = make(lambda r: httpx.Response(next(seq), json={"ok": True}), clock, sleeper)
    assert run(http.post_json("https://x.test", {})).data == {"ok": True}
    assert sleeper.calls == [1.0, 2.0]


def test_retries_are_bounded(clock, sleeper):
    count = []

    def handler(r):
        count.append(1)
        return httpx.Response(500)

    http = make(handler, clock, sleeper, attempts=4)
    with pytest.raises(HttpStatusError) as ei:
        run(http.post_json("https://x.test", {}))
    assert len(count) == 4 and ei.value.retryable and len(sleeper.calls) == 3


def test_non_retryable_4xx_fails_immediately(clock, sleeper):
    count = []

    def handler(r):
        count.append(1)
        return httpx.Response(403)

    http = make(handler, clock, sleeper)
    with pytest.raises(HttpStatusError) as ei:
        run(http.post_json("https://x.test", {}))
    assert len(count) == 1 and not ei.value.retryable and ei.value.status == 403


def test_rate_limit_honours_retry_after(clock, sleeper):
    seq = iter([httpx.Response(429, headers={"retry-after": "5"}), httpx.Response(200, json=1)])
    http = make(lambda r: next(seq), clock, sleeper)
    assert run(http.post_json("https://x.test", {})).data == 1
    assert sleeper.calls == [5.0]


def test_rate_limit_longer_than_bound_is_surfaced_not_waited(clock, sleeper):
    http = make(lambda r: httpx.Response(429, headers={"retry-after": "3600"}), clock, sleeper)
    with pytest.raises(RateLimited) as ei:
        run(http.post_json("https://x.test", {}))
    assert ei.value.retry_after == 3600 and sleeper.calls == []


def test_rate_limit_exhaustion_raises_rate_limited(clock, sleeper):
    http = make(lambda r: httpx.Response(429), clock, sleeper)
    with pytest.raises(RateLimited):
        run(http.post_json("https://x.test", {}))
    assert len(sleeper.calls) == 2


def test_timeout_classified_and_retried(clock, sleeper):
    def handler(r):
        raise httpx.ReadTimeout("slow", request=r)

    http = make(handler, clock, sleeper)
    with pytest.raises(ProviderTimeout):
        run(http.post_json("https://x.test", {}))
    assert len(sleeper.calls) == 2


def test_transport_error_redacts_url(clock, sleeper):
    def handler(r):
        raise httpx.ConnectError("failed to reach https://rpc.test/?api-key=SECRET", request=r)

    http = make(handler, clock, sleeper)
    with pytest.raises(TransportError) as ei:
        run(http.post_json("https://x.test", {}))
    assert "SECRET" not in ei.value.message


def test_invalid_json_is_malformed_not_retried(clock, sleeper):
    http = make(lambda r: httpx.Response(200, content=b"<html>"), clock, sleeper)
    with pytest.raises(MalformedResponse):
        run(http.post_json("https://x.test", {}))
    assert sleeper.calls == []


def test_floats_parse_as_decimal(clock, sleeper):
    from decimal import Decimal

    http = make(lambda r: httpx.Response(200, content=b'{"p": 0.1}'), clock, sleeper)
    assert run(http.post_json("https://x.test", {})).data["p"] == Decimal("0.1")


def test_client_side_rate_limiter_spaces_requests():
    sl = Sleeper()
    t = [0.0]
    limiter = RateLimiter(2.0, sl, now=lambda: t[0])

    async def go():
        for _ in range(3):
            await limiter.acquire()

    run(go())
    assert sl.calls == [0.5, 1.0]


@pytest.mark.parametrize(
    "kw",
    [
        dict(max_attempts=0),
        dict(timeout_seconds=0),
        dict(backoff_base_seconds=5, backoff_max_seconds=1),
    ],
)
def test_invalid_policy_rejected(kw):
    with pytest.raises(ValueError):
        RetryPolicy(**kw)
