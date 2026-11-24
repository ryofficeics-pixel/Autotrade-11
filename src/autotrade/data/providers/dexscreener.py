"""DEX Screener adapter (primary market data). PROVIDER-REPORTED values.

Contract verified against https://docs.dexscreener.com/api/reference and live calls:
  GET {base}/tokens/v1/solana/{mint}  (up to 30 comma-separated addresses; 300 req/min)
  -> JSON array of pair objects; unknown token -> []. No API key. Pair fields used:
  chainId, dexId, pairAddress, baseToken{address}, quoteToken{address}, priceNative (string),
  priceUsd (string, optional), liquidity{usd} (optional), volume{h24}, txns{m5,h1,h24}{buys,sells},
  fdv/marketCap (optional), pairCreatedAt (ms epoch, optional).
The API exposes no per-pair update time: observed_at is the collection time.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from autotrade.domain.errors import ProviderError
from autotrade.domain.models import DataQuality, MarketSnapshot, Pool
from autotrade.domain.timeutil import from_unix

from .base import FetchResult, MarketObservation
from .rest import RestProvider, expect

HEALTH_PROBE_MINT = "So11111111111111111111111111111111111111112"


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


class DexScreenerProvider(RestProvider):
    async def get_market(self, mint: str) -> FetchResult[MarketObservation]:
        from autotrade.domain.addresses import check_pubkey
        from autotrade.domain.errors import InvalidRequest

        try:
            check_pubkey(mint)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        res = await self._get(f"/tokens/v1/solana/{mint}")
        try:
            pairs = expect(res.data, list, "dexscreener tokens response")
        except ProviderError as err:
            self._fail(err)
            raise
        items: list[MarketObservation] = []
        rejections: list[str] = []
        for pair in pairs:
            # Only pairs where `mint` is the base token: priceNative is quoted per base token.
            if _get(pair, "baseToken", "address") != mint:
                continue
            try:
                items.append(self._parse(pair, res.received_at))
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejections.append(f"{_get(pair, 'pairAddress')}: {exc}"[:300])
        return FetchResult(items=items, rejections=rejections, fetched=len(pairs))

    def _parse(self, p: dict[str, Any], received_at: Any) -> MarketObservation:
        if p["chainId"] != "solana":
            raise ValueError(f"unexpected chain {p['chainId']!r}")
        notes: list[str] = []
        liquidity = _get(p, "liquidity", "usd")
        if liquidity is None:
            notes.append("liquidity_usd not reported")
        created_ms = p.get("pairCreatedAt")
        common: dict[str, Any] = {
            "source": self.name,
            "observed_at": received_at,
            "received_at": received_at,
            "quality": DataQuality.PARTIAL if notes else DataQuality.OK,
            "quality_notes": tuple(notes),
        }
        base, quote = p["baseToken"]["address"], p["quoteToken"]["address"]
        created = from_unix(created_ms / 1000) if isinstance(created_ms, int) else None
        pool = Pool(
            address=p["pairAddress"],
            dex=p["dexId"],
            base_mint=base,
            quote_mint=quote,
            created_at=created,
            **common,
        )
        txns = p.get("txns") or {}
        snap = MarketSnapshot(
            pool_address=p["pairAddress"],
            base_mint=base,
            quote_mint=quote,
            price_quote=p["priceNative"],
            price_usd=p.get("priceUsd"),
            liquidity_usd=liquidity,
            volume_24h_usd=_get(p, "volume", "h24"),
            fdv_usd=p.get("fdv"),
            market_cap_usd=p.get("marketCap"),
            buys_5m=_get(txns, "m5", "buys"),
            sells_5m=_get(txns, "m5", "sells"),
            buys_1h=_get(txns, "h1", "buys"),
            sells_1h=_get(txns, "h1", "sells"),
            buys_24h=_get(txns, "h24", "buys"),
            sells_24h=_get(txns, "h24", "sells"),
            pair_created_at=created,
            **common,
        )
        return MarketObservation(pool, snap)

    async def check_health(self) -> Any:
        try:
            await self.get_market(HEALTH_PROBE_MINT)
        except ProviderError:
            pass  # recorded by the tracker
        return self.health()
