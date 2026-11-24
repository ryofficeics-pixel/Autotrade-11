"""Shared deterministic fixtures: fake clock, mocked RPC transport, no network, no real sleeps."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from autotrade.data.providers.http import RateLimiter, ResilientHttp, RetryPolicy
from autotrade.data.providers.solana_rpc import SolanaRpcClient
from autotrade.data.storage.db import Database

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MINT = "So11111111111111111111111111111111111111112"
MINT2 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
WALLET2 = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def sig(n: int) -> str:
    return b58(bytes([n % 250 + 1]) * 64)


async def _noop(_: float) -> None:
    return None


def _free_limiter() -> RateLimiter:
    return RateLimiter(1_000_000, _noop)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


def mint_account(
    *,
    decimals: int = 6,
    mint_authority: str | None = None,
    supply: str = "1000000",
    program: str = "spl-token",
) -> dict[str, Any]:
    return {
        "data": {
            "program": program,
            "space": 82,
            "parsed": {
                "type": "mint",
                "info": {
                    "decimals": decimals,
                    "freezeAuthority": None,
                    "isInitialized": True,
                    "mintAuthority": mint_authority,
                    "supply": supply,
                },
            },
        },
        "executable": False,
        "lamports": 1461600,
        "owner": TOKEN_PROGRAM,
        "rentEpoch": 0,
        "space": 82,
    }


Handler = Callable[[str, list[Any]], Any]


def rpc_transport(
    handler: Handler, calls: list[tuple[str, list[Any]]] | None = None
) -> httpx.MockTransport:
    """handler(method, params) -> JSON-RPC result, or an httpx.Response / Exception to return."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append((body["method"], body["params"]))
        out = handler(body["method"], body["params"])
        if isinstance(out, httpx.Response):
            return out
        if isinstance(out, Exception):
            raise out
        if isinstance(out, dict) and "__rpc_error__" in out:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": out["__rpc_error__"]}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": out})

    return httpx.MockTransport(respond)


def make_rpc(
    transport: httpx.MockTransport,
    clock: FakeClock,
    sleeper: Sleeper,
    *,
    endpoints: list[str] | None = None,
    attempts: int = 3,
) -> SolanaRpcClient:
    http = ResilientHttp(
        RetryPolicy(
            timeout_seconds=1,
            max_attempts=attempts,
            backoff_base_seconds=1,
            backoff_max_seconds=8,
            max_requests_per_second=1000,
        ),
        client=httpx.AsyncClient(transport=transport),
        clock=clock,
        sleep=sleeper,
        rng=lambda: 1.0,
        limiter=_free_limiter(),
    )
    return SolanaRpcClient(
        endpoints or ["https://rpc.example.test/?api-key=SECRET"], http, clock=clock
    )


@pytest.fixture
def db(tmp_path, clock) -> Database:
    database = Database(tmp_path / "t.sqlite3", clock=clock)
    with database.session() as conn:
        database.migrate(conn)
    return database


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def make_rest(
    cls,
    handler,
    clock,
    sleeper,
    *,
    name="p",
    base="https://api.example.test",
    headers=None,
    attempts=3,
):  # type: ignore[no-untyped-def]
    http = ResilientHttp(
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
    return cls(name, base, http, clock, headers)
