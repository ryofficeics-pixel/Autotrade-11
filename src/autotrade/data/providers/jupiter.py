"""Jupiter Tokens API v2 adapter (secondary). PROVIDER-REPORTED values.

Contract verified against https://dev.jup.ag/docs and live calls (no key needed at time of
writing; an optional key is sent as `x-api-key`):
  GET {base}/tokens/v2/search?query={mint} -> JSON array of token objects (query may fuzzy-match,
  so only the object whose `id` equals the mint is accepted; none -> []).
  Fields used: id, name, symbol, decimals, usdPrice, liquidity, mcap, fdv, holderCount,
  organicScore, isVerified, tags[], updatedAt (ISO-8601), audit{mintAuthorityDisabled,
  freezeAuthorityDisabled, topHoldersPercentage}. `audit` can be absent for some tokens.
Default base is https://lite-api.jup.ag (documented free tier; limits not guaranteed).
`/price/v3?ids=` exists and was verified live but is not called: the search result already
carries usdPrice, and extra calls would waste the public rate limit.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from autotrade.domain.addresses import check_pubkey
from autotrade.domain.errors import HttpStatusError, InvalidRequest, ProviderError
from autotrade.domain.models import DataQuality, SellQuote, TokenIntel
from autotrade.domain.timeutil import to_utc

from .base import FetchResult
from .dexscreener import HEALTH_PROBE_MINT
from .rest import RestProvider, expect

WSOL = "So11111111111111111111111111111111111111112"
NO_ROUTE_CODES = frozenset({"TOKEN_NOT_TRADABLE", "COULD_NOT_FIND_ANY_ROUTE", "NO_ROUTES_FOUND"})


class JupiterProvider(RestProvider):
    async def get_intel(self, mint: str) -> FetchResult[TokenIntel]:
        try:
            check_pubkey(mint)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        res = await self._get("/tokens/v2/search", {"query": mint})
        try:
            rows = expect(res.data, list, "jupiter search response")
        except ProviderError as err:
            self._fail(err)
            raise
        match = [r for r in rows if isinstance(r, dict) and r.get("id") == mint]
        if not match:
            return FetchResult(rejections=[f"{mint}: not listed by jupiter"], fetched=len(rows))
        try:
            return FetchResult(items=[self._parse(match[0], res.received_at)], fetched=len(rows))
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            return FetchResult(
                rejections=[f"{mint}: invalid token: {exc}"[:300]], fetched=len(rows)
            )

    def _parse(self, t: dict[str, Any], received_at: Any) -> TokenIntel:
        notes: list[str] = []
        updated = t.get("updatedAt")
        # Jupiter's own update time is the observation time; fall back to collection time.
        observed = min(to_utc(updated), received_at) if updated else received_at
        if not updated:
            notes.append("updatedAt missing; observed_at is collection time")
        audit = t.get("audit")
        if not isinstance(audit, dict):
            audit = {}
            notes.append("audit block missing")
        return TokenIntel(
            source=self.name,
            observed_at=observed,
            received_at=received_at,
            quality=DataQuality.PARTIAL if notes else DataQuality.OK,
            quality_notes=tuple(notes),
            mint=t["id"],
            symbol=t.get("symbol"),
            name=t.get("name"),
            decimals=t.get("decimals"),
            is_verified=t.get("isVerified"),
            tags=tuple(t.get("tags") or ()),
            mint_authority_disabled=audit.get("mintAuthorityDisabled"),
            freeze_authority_disabled=audit.get("freezeAuthorityDisabled"),
            top_holders_pct=audit.get("topHoldersPercentage"),
            holder_count=t.get("holderCount"),
            organic_score=t.get("organicScore"),
            usd_price=t.get("usdPrice"),
            liquidity_usd=t.get("liquidity"),
            market_cap_usd=t.get("mcap"),
            fdv_usd=t.get("fdv"),
        )

    async def check_health(self) -> Any:
        try:
            await self.get_intel(HEALTH_PROBE_MINT)
        except ProviderError:
            pass
        return self.health()

    async def get_sell_quote(self, mint: str, amount_raw: int) -> FetchResult[SellQuote]:
        """GET {base}/swap/v1/quote?inputMint={mint}&outputMint=wSOL&amount={raw}&slippageBps=100
        (verified live): 200 -> {inAmount, outAmount, priceImpactPct, routePlan[].swapInfo.label};
        400 -> {"error", "errorCode"} e.g. TOKEN_NOT_TRADABLE / COULD_NOT_FIND_ANY_ROUTE.
        Only the quote endpoint is used; the swap/transaction endpoints are never called."""
        try:
            check_pubkey(mint)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        if amount_raw <= 0:
            raise InvalidRequest("amount must be positive")
        params = {
            "inputMint": mint,
            "outputMint": WSOL,
            "amount": str(amount_raw),
            "slippageBps": "100",
        }
        try:
            res = await self._get("/swap/v1/quote", params)
        except HttpStatusError as err:
            code = err.body.get("errorCode") if isinstance(err.body, dict) else None
            if err.status == 400 and code in NO_ROUTE_CODES:
                now = self._clock()
                quote = SellQuote(
                    source=self.name,
                    observed_at=now,
                    received_at=now,
                    mint=mint,
                    in_amount_raw=amount_raw,
                    routable=False,
                    error_code=code,
                )
                return FetchResult(items=[quote], fetched=1)
            raise
        try:
            d = expect(res.data, dict, "jupiter quote response")
            out = int(d["outAmount"])
            labels = tuple(
                str(step["swapInfo"]["label"]) for step in expect(d["routePlan"], list, "routePlan")
            )
            impact = d.get("priceImpactPct")
            quote = SellQuote(
                source=self.name,
                observed_at=res.received_at,
                received_at=res.received_at,
                mint=mint,
                in_amount_raw=int(d["inAmount"]),
                routable=out > 0 and bool(labels),
                out_amount_raw=out if out > 0 and labels else None,
                price_impact_pct=impact,
                route_labels=labels if out > 0 else (),
                error_code=None if out > 0 and labels else "EMPTY_ROUTE",
            )
        except ProviderError as err:
            self._fail(err)
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            return FetchResult(rejections=[f"{mint}: invalid quote: {exc}"[:300]], fetched=1)
        return FetchResult(items=[quote], fetched=1)
