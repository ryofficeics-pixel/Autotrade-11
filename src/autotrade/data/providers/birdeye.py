"""Birdeye OHLCV adapter (OPTIONAL; needs the user's own API key, never required).

Contract per https://docs.birdeye.so (OHLCV):
  GET https://public-api.birdeye.so/defi/ohlcv?address={mint}&type={1m|5m|15m|1H|...}
      &time_from={unix s}&time_to={unix s}      headers: X-API-KEY, x-chain: solana
  -> {"success": true, "data": {"items": [{"o","h","l","c","v","unixTime","address","type",
     "currency"}...]}}
Verified live: the endpoint answers 401 {"success":false,"message":"Unauthorized"} without a
key. Parsing of the success shape follows the documented example and is fixture-tested only,
because no key was available to exercise it.
Only CLOSED candles are returned; a still-forming candle is dropped, never stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from autotrade.domain.addresses import check_pubkey
from autotrade.domain.errors import InvalidRequest, MalformedResponse, ProviderError
from autotrade.domain.models import Candle
from autotrade.domain.timeutil import from_unix

from .base import FetchResult
from .rest import RestProvider, expect

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
}


class BirdeyeProvider(RestProvider):
    async def get_candles(
        self, mint: str, interval: str, time_from: datetime, time_to: datetime
    ) -> FetchResult[Candle]:
        try:
            check_pubkey(mint)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        if interval not in INTERVAL_SECONDS:
            raise InvalidRequest(f"unsupported interval {interval!r}")
        if time_from >= time_to:
            raise InvalidRequest("time_from must be before time_to")
        res = await self._get(
            "/defi/ohlcv",
            {
                "address": mint,
                "type": interval,
                "time_from": int(time_from.timestamp()),
                "time_to": int(time_to.timestamp()),
            },
        )
        try:
            body = expect(res.data, dict, "birdeye ohlcv response")
            if body.get("success") is not True:
                raise MalformedResponse("birdeye success flag is not true")
            rows = expect(_path(body, "data", "items"), list, "birdeye data.items")
        except ProviderError as err:
            self._fail(err)
            raise
        span = timedelta(seconds=INTERVAL_SECONDS[interval])
        items: list[Candle] = []
        rejections: list[str] = []
        for row in rows:
            try:
                open_time = from_unix(row["unixTime"])
                if open_time + span > res.received_at:
                    continue  # still forming; not final
                items.append(
                    Candle(
                        source=self.name,
                        observed_at=open_time + span,
                        received_at=res.received_at,
                        mint=mint,
                        interval=interval,
                        open_time=open_time,
                        open=row["o"],
                        high=row["h"],
                        low=row["l"],
                        close=row["c"],
                        volume=row["v"],
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejections.append(f"{mint}: invalid candle: {exc}"[:300])
        return FetchResult(items=items, rejections=rejections, fetched=len(rows))


def _path(d: Any, *keys: str) -> Any:
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d
