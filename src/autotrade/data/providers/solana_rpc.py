"""Read-only Solana JSON-RPC client (documented methods only).

Docs: https://solana.com/docs/rpc  — methods used: getSlot, getAccountInfo,
getTransaction, getTokenAccountBalance, getSignaturesForAddress.
Anything outside the allowlist is refused locally; there is no signing or broadcast path.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from autotrade.domain.addresses import check_pubkey, check_signature
from autotrade.domain.errors import (
    InvalidRequest,
    MalformedResponse,
    ProviderError,
    ProviderUnavailable,
    RpcError,
)
from autotrade.domain.models import ProviderHealth
from autotrade.domain.timeutil import Clock, utcnow

from .health import HealthTracker
from .http import ResilientHttp

ALLOWED_METHODS = frozenset(
    {
        "getSlot",
        "getAccountInfo",
        "getTransaction",
        "getTokenAccountBalance",
        "getSignaturesForAddress",
    }
)
COMMITMENTS = frozenset({"processed", "confirmed", "finalized"})


def endpoint_label(url: str) -> str:
    """Host only: URLs may embed API keys."""
    return urlsplit(url).hostname or "unknown"


@dataclass(frozen=True)
class RpcResponse:
    value: Any
    context_slot: int | None
    requested_at: datetime
    received_at: datetime
    endpoint: str


def parse_rpc_envelope(data: Any, request_id: int) -> Any:
    """Validate a JSON-RPC 2.0 response; return result or raise a classified error."""
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        raise MalformedResponse("not a JSON-RPC 2.0 object")
    if data.get("id") != request_id:
        raise MalformedResponse("JSON-RPC id mismatch")
    has_result, has_error = "result" in data, "error" in data
    if has_result == has_error:
        raise MalformedResponse("response must contain exactly one of result/error")
    if has_error:
        err = data["error"]
        if (
            not isinstance(err, dict)
            or not isinstance(err.get("code"), int)
            or isinstance(err.get("code"), bool)
            or not isinstance(err.get("message"), str)
        ):
            raise MalformedResponse("malformed JSON-RPC error object")
        raise RpcError(err["code"], err["message"][:200])
    return data["result"]


class SolanaRpcClient:
    name = "solana_rpc"

    def __init__(
        self,
        endpoints: list[str],
        http: ResilientHttp,
        *,
        commitment: str = "confirmed",
        clock: Clock = utcnow,
    ) -> None:
        if not endpoints:
            raise ValueError("at least one RPC endpoint is required")
        if commitment not in COMMITMENTS:
            raise ValueError(f"invalid commitment: {commitment}")
        self._endpoints = list(endpoints)
        self._http = http
        self.commitment = commitment
        self._ids = itertools.count(1)
        self._tracker = HealthTracker(self.name, endpoint_label(endpoints[0]), clock)

    def health(self) -> ProviderHealth:
        return self._tracker.snapshot()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, params: list[Any]) -> RpcResponse:
        if method not in ALLOWED_METHODS:
            raise InvalidRequest(f"RPC method not permitted (read-only allowlist): {method}")
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        last: ProviderError | None = None
        for url in self._endpoints:
            try:
                result = await self._http.post_json(
                    url, payload, functools.partial(parse_rpc_envelope, request_id=request_id)
                )
            except ProviderError as err:
                last = err
                if not err.retryable:
                    self._tracker.record_failure(err)
                    raise  # logical error: another endpoint will not help
                continue  # transient after bounded retries: try fallback endpoint
            self._tracker.record_success(result.latency_ms)
            self._tracker.endpoint = endpoint_label(url)  # report who actually answered
            value, slot = result.data, None
            if isinstance(value, dict) and "context" in value and "value" in value:
                ctx = value["context"]
                slot = ctx.get("slot") if isinstance(ctx, dict) else None
                value = value["value"]
            return RpcResponse(
                value,
                slot if isinstance(slot, int) else None,
                result.requested_at,
                result.received_at,
                endpoint_label(url),
            )
        assert last is not None
        self._tracker.record_failure(last)
        raise ProviderUnavailable(f"all RPC endpoints failed; last: {last.message}") from last

    # ---- typed read-only operations -------------------------------------------------
    async def get_slot(self) -> RpcResponse:
        resp = await self.call("getSlot", [{"commitment": self.commitment}])
        if not isinstance(resp.value, int) or isinstance(resp.value, bool) or resp.value < 0:
            raise MalformedResponse("getSlot result is not a non-negative integer")
        return resp

    async def get_account_info(self, address: str, *, encoding: str = "jsonParsed") -> RpcResponse:
        _require(check_pubkey, address)
        return await self.call(
            "getAccountInfo", [address, {"encoding": encoding, "commitment": self.commitment}]
        )

    async def get_token_account_balance(self, address: str) -> RpcResponse:
        _require(check_pubkey, address)
        return await self.call("getTokenAccountBalance", [address, {"commitment": self.commitment}])

    async def get_transaction(self, signature: str) -> RpcResponse:
        _require(check_signature, signature)
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": self.commitment,
                },
            ],
        )

    async def get_signatures_for_address(
        self, address: str, *, limit: int = 50, before: str | None = None
    ) -> RpcResponse:
        _require(check_pubkey, address)
        if not 1 <= limit <= 1000:
            raise InvalidRequest("limit must be within 1..1000")
        opts: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before is not None:
            _require(check_signature, before)
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts])

    async def check_health(self) -> ProviderHealth:
        """Health = a real getSlot round trip; failures are recorded, not hidden."""
        try:
            await self.get_slot()
        except ProviderError:
            pass  # outcome is captured in the tracker; snapshot reports it
        return self.health()


def _require(check: Callable[[str], str], value: str) -> None:
    try:
        check(value)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
