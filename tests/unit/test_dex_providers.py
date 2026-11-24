import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from autotrade.data.providers.base import (
    DexMarketDataProvider,
    OhlcvProvider,
    TokenIntelProvider,
)
from autotrade.data.providers.birdeye import BirdeyeProvider
from autotrade.data.providers.dexscreener import DexScreenerProvider
from autotrade.data.providers.jupiter import JupiterProvider
from autotrade.domain.errors import (
    HttpStatusError,
    InvalidRequest,
    MalformedResponse,
    ProviderTimeout,
    RateLimited,
)
from autotrade.domain.models import DataQuality, HealthStatus
from tests.conftest import MINT, MINT2, WALLET, fixture, make_rest

run = asyncio.run
DEX_FIX = fixture("dexscreener_tokens_wsol.json")
JUP_FIX = fixture("jupiter_search_wsol.json")


def dex(handler, clock, sleeper, **kw):
    return make_rest(DexScreenerProvider, handler, clock, sleeper, name="dexscreener", **kw)


def jup(handler, clock, sleeper, **kw):
    return make_rest(JupiterProvider, handler, clock, sleeper, name="jupiter", **kw)


def test_contracts_satisfied(clock, sleeper):
    h = lambda r: httpx.Response(200, json=[])  # noqa: E731
    assert isinstance(dex(h, clock, sleeper), DexMarketDataProvider)
    assert isinstance(jup(h, clock, sleeper), TokenIntelProvider)
    assert isinstance(make_rest(BirdeyeProvider, h, clock, sleeper), OhlcvProvider)


# ---- DEX Screener --------------------------------------------------------------------
def test_dexscreener_parses_recorded_response_and_request_shape(clock, sleeper):
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=DEX_FIX)

    res = run(dex(handler, clock, sleeper).get_market(MINT))
    assert seen[0].method == "GET" and seen[0].url.path == f"/tokens/v1/solana/{MINT}"
    obs = res.items[0]
    s, pool = obs.snapshot, obs.pool
    assert s.base_mint == MINT and s.price_quote == Decimal("70.4349")
    assert s.price_usd == Decimal("70.43") and s.liquidity_usd == Decimal("17302576.45")
    assert (s.buys_1h, s.sells_1h, s.buys_24h) == (1397, 3022, 54488)
    assert s.volume_24h_usd == Decimal("123956224.98")
    assert s.fdv_usd is None  # not reported => explicit None, never zero
    assert s.observed_at == s.received_at == clock.now and s.source == "dexscreener"
    assert pool.dex == "orca" and pool.created_at.year == 2023 and s.quality is DataQuality.OK


def test_dexscreener_ignores_pairs_where_mint_is_quote(clock, sleeper):
    res = run(dex(lambda r: httpx.Response(200, json=DEX_FIX), clock, sleeper).get_market(MINT2))
    assert res.items == [] and res.fetched == 1


def test_dexscreener_unknown_token_is_empty_not_error(clock, sleeper):
    res = run(dex(lambda r: httpx.Response(200, json=[]), clock, sleeper).get_market(MINT))
    assert res.items == [] and res.rejections == []


def test_dexscreener_missing_liquidity_marks_partial(clock, sleeper):
    pair = {**DEX_FIX[0], "liquidity": None}
    res = run(dex(lambda r: httpx.Response(200, json=[pair]), clock, sleeper).get_market(MINT))
    snap = res.items[0].snapshot
    assert snap.liquidity_usd is None and snap.quality is DataQuality.PARTIAL


@pytest.mark.parametrize(
    "patch",
    [
        {"priceNative": "0"},
        {"priceNative": "abc"},
        {"priceNative": None},
        {"chainId": "ethereum"},
        {"pairAddress": "bad"},
        {"liquidity": {"usd": -5}},
    ],
)
def test_dexscreener_rejects_invalid_pairs(clock, sleeper, patch):
    pair = {**DEX_FIX[0], **patch}
    res = run(dex(lambda r: httpx.Response(200, json=[pair]), clock, sleeper).get_market(MINT))
    assert res.items == [] and len(res.rejections) == 1


def test_dexscreener_wrong_shape_is_malformed_and_unhealthy(clock, sleeper):
    p = dex(lambda r: httpx.Response(200, json={"pairs": []}), clock, sleeper)
    with pytest.raises(MalformedResponse):
        run(p.get_market(MINT))
    assert p.health().consecutive_failures == 1


def test_dexscreener_rate_limit_retry_then_ok(clock, sleeper):
    seq = iter([httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json=[])])
    p = dex(lambda r: next(seq), clock, sleeper)
    run(p.get_market(MINT))
    assert sleeper.calls == [2.0] and p.health().status is HealthStatus.HEALTHY


def test_dexscreener_persistent_429_and_timeout_and_5xx(clock, sleeper):
    p = dex(lambda r: httpx.Response(429), clock, sleeper)
    with pytest.raises(RateLimited):
        run(p.get_market(MINT))

    def slow(r):
        raise httpx.ReadTimeout("t", request=r)

    with pytest.raises(ProviderTimeout):
        run(dex(slow, clock, sleeper).get_market(MINT))
    p = dex(lambda r: httpx.Response(503), clock, sleeper, attempts=2)
    for _ in range(3):
        with pytest.raises(HttpStatusError):
            run(p.get_market(MINT))
    assert p.health().status is HealthStatus.DOWN


def test_dexscreener_invalid_address_never_hits_network(clock, sleeper):
    called = []
    p = dex(lambda r: called.append(1) or httpx.Response(200, json=[]), clock, sleeper)
    with pytest.raises(InvalidRequest):
        run(p.get_market("../../etc"))
    assert called == []


# ---- Jupiter -------------------------------------------------------------------------
def test_jupiter_parses_recorded_response(clock, sleeper):
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=JUP_FIX)

    clock.now = clock.now.replace(year=2027)  # recorded updatedAt is in the past
    res = run(jup(handler, clock, sleeper, headers={"x-api-key": "k"}).get_intel(MINT))
    assert seen[0].url.path == "/tokens/v2/search" and seen[0].url.params["query"] == MINT
    assert seen[0].headers["x-api-key"] == "k"
    t = res.items[0]
    assert (t.symbol, t.decimals, t.is_verified) == ("SOL", 9, True)
    assert t.mint_authority_disabled is True and t.freeze_authority_disabled is True
    assert t.usd_price == Decimal("70.48521953022977") and t.top_holders_pct > 0
    assert t.observed_at.year == 2026 and t.observed_at < t.received_at  # provider's own time
    assert "strict" in t.tags


def test_jupiter_only_exact_mint_match_accepted(clock, sleeper):
    other = {**JUP_FIX[0], "id": MINT2}
    p = jup(lambda r: httpx.Response(200, json=[other]), clock, sleeper)
    res = run(p.get_intel(MINT))
    assert res.items == [] and "not listed" in res.rejections[0]


def test_jupiter_missing_audit_is_unknown_partial(clock, sleeper):
    tok = {k: v for k, v in JUP_FIX[0].items() if k != "audit"}
    clock.now = clock.now.replace(year=2027)
    res = run(jup(lambda r: httpx.Response(200, json=[tok]), clock, sleeper).get_intel(MINT))
    t = res.items[0]
    assert t.mint_authority_disabled is None and t.quality is DataQuality.PARTIAL


def test_jupiter_rejects_invalid_values(clock, sleeper):
    tok = {**JUP_FIX[0], "usdPrice": -1}
    res = run(jup(lambda r: httpx.Response(200, json=[tok]), clock, sleeper).get_intel(MINT))
    assert res.items == [] and res.rejections


def test_jupiter_future_update_time_is_clamped_to_receipt(clock, sleeper):
    tok = {**JUP_FIX[0], "updatedAt": (clock.now + timedelta(days=3)).isoformat()}
    res = run(jup(lambda r: httpx.Response(200, json=[tok]), clock, sleeper).get_intel(MINT))
    assert res.items[0].observed_at == clock.now


def test_jupiter_failures_classified(clock, sleeper):
    with pytest.raises(HttpStatusError) as ei:
        run(jup(lambda r: httpx.Response(401), clock, sleeper).get_intel(MINT))
    assert not ei.value.retryable
    with pytest.raises(MalformedResponse):
        run(jup(lambda r: httpx.Response(200, json={"a": 1}), clock, sleeper).get_intel(MINT))
    with pytest.raises(RateLimited):
        run(jup(lambda r: httpx.Response(429), clock, sleeper).get_intel(MINT))


# ---- Birdeye (optional) ---------------------------------------------------------------
def bird(handler, clock, sleeper):
    return make_rest(
        BirdeyeProvider,
        handler,
        clock,
        sleeper,
        name="birdeye",
        headers={"X-API-KEY": "k", "x-chain": "solana"},
    )


def test_birdeye_parses_documented_shape_and_sends_headers(clock, sleeper):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json=fixture("birdeye_ohlcv.json"))

    clock.now = clock.now.replace(year=2027)
    end = clock.now
    res = run(bird(handler, clock, sleeper).get_candles(MINT, "15m", end - timedelta(hours=1), end))
    r = seen[0]
    assert r.url.path == "/defi/ohlcv" and r.url.params["type"] == "15m"
    assert r.headers["x-api-key"] == "k" and r.headers["x-chain"] == "solana"
    assert [int(r.url.params["time_to"]) - int(r.url.params["time_from"])] == [3600]
    c = res.items[0]
    assert (c.open, c.high, c.low, c.close) == (
        Decimal("127.1"),
        Decimal("127.9"),
        Decimal("126.8"),
        Decimal("127.5"),
    )
    assert len(res.items) == 2 and c.observed_at == c.open_time + timedelta(minutes=15)


def test_birdeye_drops_unclosed_and_rejects_bad_candles(clock, sleeper):
    now = int(clock.now.timestamp())
    rows = [
        {"o": 1, "h": 2, "l": 1, "c": 2, "v": 1, "unixTime": now - 60},  # still forming
        {"o": 1, "h": 1, "l": 2, "c": 1, "v": 1, "unixTime": now - 7200},  # low > high
        {"o": 1, "h": 2, "l": 1, "c": 2, "unixTime": now - 7200},  # no volume
        {"o": 1, "h": 2, "l": 1, "c": 2, "v": 1, "unixTime": now - 7200},
    ]
    p = bird(
        lambda r: httpx.Response(200, json={"success": True, "data": {"items": rows}}),
        clock,
        sleeper,
    )
    res = run(p.get_candles(MINT, "15m", clock.now - timedelta(hours=3), clock.now))
    assert len(res.items) == 1 and len(res.rejections) == 2 and res.fetched == 4


def test_birdeye_unauthorized_and_bad_inputs(clock, sleeper):
    p = bird(
        lambda r: httpx.Response(401, json={"success": False, "message": "Unauthorized"}),
        clock,
        sleeper,
    )
    with pytest.raises(HttpStatusError) as ei:
        run(p.get_candles(MINT, "15m", clock.now - timedelta(hours=1), clock.now))
    assert ei.value.status == 401 and not ei.value.retryable
    for args in [
        (MINT, "7m", clock.now - timedelta(hours=1), clock.now),
        (MINT, "15m", clock.now, clock.now),
        (WALLET[:5], "15m", clock.now, clock.now),
    ]:
        with pytest.raises(InvalidRequest):
            run(p.get_candles(*args))
    ok_false = bird(lambda r: httpx.Response(200, json={"success": False}), clock, sleeper)
    with pytest.raises(MalformedResponse):
        run(ok_false.get_candles(MINT, "15m", clock.now - timedelta(hours=1), clock.now))
