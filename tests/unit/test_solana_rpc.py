import asyncio

import httpx
import pytest

from autotrade.data.providers.rpc_adapters import (
    RpcTokenMetadataProvider,
    RpcWalletActivityProvider,
)
from autotrade.data.providers.solana_rpc import endpoint_label, parse_rpc_envelope
from autotrade.domain.errors import (
    InvalidRequest,
    MalformedResponse,
    ProviderUnavailable,
    RpcError,
)
from autotrade.domain.models import DataQuality, HealthStatus
from tests.conftest import MINT, WALLET, make_rpc, mint_account, rpc_transport, sig

run = asyncio.run
CTX = lambda v: {"context": {"slot": 77}, "value": v}  # noqa: E731


def test_envelope_validation():
    assert parse_rpc_envelope({"jsonrpc": "2.0", "id": 1, "result": 5}, 1) == 5
    for bad in (
        None,
        [],
        {"jsonrpc": "1.0", "id": 1, "result": 1},
        {"jsonrpc": "2.0", "id": 2, "result": 1},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": 1, "error": {"code": 1, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "x", "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": "boom"},
    ):
        with pytest.raises(MalformedResponse):
            parse_rpc_envelope(bad, 1)


@pytest.mark.parametrize(
    "code,retryable",
    [(-32005, True), (-32004, True), (-32602, False), (-32601, False), (-32009, False)],
)
def test_rpc_error_classification(code, retryable):
    err = RpcError(code, "x")
    assert err.retryable is retryable


def test_get_slot_and_health(clock, sleeper):
    rpc = make_rpc(rpc_transport(lambda m, p: 424242), clock, sleeper)
    assert run(rpc.get_slot()).value == 424242
    h = run(rpc.check_health())
    assert h.status is HealthStatus.HEALTHY and h.endpoint == "rpc.example.test"
    assert "SECRET" not in repr(h)


def test_get_slot_rejects_non_integer(clock, sleeper):
    rpc = make_rpc(rpc_transport(lambda m, p: "ok"), clock, sleeper)
    with pytest.raises(MalformedResponse):
        run(rpc.get_slot())


def test_context_slot_extracted(clock, sleeper):
    rpc = make_rpc(rpc_transport(lambda m, p: CTX(mint_account())), clock, sleeper)
    resp = run(rpc.get_account_info(MINT))
    assert resp.context_slot == 77 and resp.endpoint == "rpc.example.test"
    assert resp.received_at == clock.now


def test_logical_rpc_error_not_retried_and_no_fallback(clock, sleeper):
    calls = []
    t = rpc_transport(lambda m, p: {"__rpc_error__": {"code": -32602, "message": "bad"}}, calls)
    rpc = make_rpc(t, clock, sleeper, endpoints=["https://a.test", "https://b.test"])
    with pytest.raises(RpcError):
        run(rpc.get_slot())
    assert len(calls) == 1


def test_transient_failure_falls_back_to_second_endpoint(clock, sleeper):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.test":
            return httpx.Response(503)
        import json

        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 9})

    rpc = make_rpc(
        httpx.MockTransport(handler), clock, sleeper, endpoints=["https://a.test", "https://b.test"]
    )
    resp = run(rpc.get_slot())
    assert resp.value == 9 and resp.endpoint == "b.test"


def test_all_endpoints_down_marks_provider_down(clock, sleeper):
    rpc = make_rpc(
        httpx.MockTransport(lambda r: httpx.Response(503)),
        clock,
        sleeper,
        endpoints=["https://a.test"],
        attempts=2,
    )
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            run(rpc.get_slot())
    h = rpc.health()
    assert h.status is HealthStatus.DOWN and h.consecutive_failures == 3
    assert h.last_error_kind == "http_status"


def test_recovery_resets_health(clock, sleeper):
    state = {"fail": True}

    def handler(m, p):
        return httpx.Response(503) if state["fail"] else 1

    rpc = make_rpc(rpc_transport(handler), clock, sleeper, attempts=1)
    with pytest.raises(ProviderUnavailable):
        run(rpc.get_slot())
    assert rpc.health().status is HealthStatus.DEGRADED
    state["fail"] = False
    run(rpc.get_slot())
    assert rpc.health().status is HealthStatus.HEALTHY


def test_invalid_inputs_never_hit_network(clock, sleeper):
    calls = []
    rpc = make_rpc(rpc_transport(lambda m, p: 1, calls), clock, sleeper)
    for coro in (
        rpc.get_account_info("bad"),
        rpc.get_transaction("bad"),
        rpc.get_signatures_for_address(WALLET, limit=0),
        rpc.get_token_account_balance("x"),
    ):
        with pytest.raises(InvalidRequest):
            run(coro)
    assert calls == []


def test_get_transaction_and_token_balance_params(clock, sleeper):
    calls = []
    rpc = make_rpc(
        rpc_transport(
            lambda m, p: None if m == "getTransaction" else CTX({"amount": "5", "decimals": 6}),
            calls,
        ),
        clock,
        sleeper,
    )
    assert run(rpc.get_transaction(sig(1))).value is None
    assert run(rpc.get_token_account_balance(WALLET)).value["amount"] == "5"
    assert calls[0][1][1]["maxSupportedTransactionVersion"] == 0


def test_endpoint_label_hides_key():
    assert endpoint_label("https://mainnet.example.com/v2/SECRETKEY") == "mainnet.example.com"


# ---- adapters ------------------------------------------------------------------------
def test_token_provider_parses_mint(clock, sleeper):
    rpc = make_rpc(
        rpc_transport(lambda m, p: CTX(mint_account(decimals=9, mint_authority=WALLET))),
        clock,
        sleeper,
    )
    res = run(RpcTokenMetadataProvider(rpc).get_token(MINT))
    t = res.items[0]
    assert (t.decimals, t.mint_authority, t.freeze_authority, t.supply_raw) == (
        9,
        WALLET,
        None,
        1000000,
    )
    assert t.authorities_known and t.symbol is None and t.observed_at == clock.now


def test_token_provider_missing_account_is_rejection_not_token(clock, sleeper):
    rpc = make_rpc(rpc_transport(lambda m, p: CTX(None)), clock, sleeper)
    res = run(RpcTokenMetadataProvider(rpc).get_token(MINT))
    assert res.items == [] and "not found" in res.rejections[0]


@pytest.mark.parametrize(
    "value",
    [
        {"data": ["", "base64"], "owner": "x"},
        {"data": {"program": "system", "parsed": {"type": "mint", "info": {}}}},
        {"data": {"program": "spl-token", "parsed": {"type": "account", "info": {}}}},
        mint_account(decimals=999),
        mint_account(supply="abc"),
    ],
)
def test_token_provider_rejects_non_mint_or_malformed(clock, sleeper, value):
    rpc = make_rpc(rpc_transport(lambda m, p: CTX(value)), clock, sleeper)
    res = run(RpcTokenMetadataProvider(rpc).get_token(MINT))
    assert res.items == [] and len(res.rejections) == 1


def test_wallet_provider_parses_and_rejects_bad_rows(clock, sleeper):
    bt = int(clock.now.timestamp()) - 30
    rows = [
        {"signature": sig(1), "slot": 10, "err": None, "blockTime": bt},
        {"signature": sig(2), "slot": 11, "err": {"InstructionError": [0, "x"]}, "blockTime": None},
        {"signature": "garbage", "slot": 12, "err": None, "blockTime": bt},
        {"slot": 13},
    ]
    rpc = make_rpc(rpc_transport(lambda m, p: rows), clock, sleeper)
    res = run(RpcWalletActivityProvider(rpc).get_activity(WALLET, 10))
    assert res.fetched == 4 and len(res.items) == 2 and len(res.rejections) == 2
    ok, failed = res.items
    assert ok.succeeded and ok.quality is DataQuality.OK and ok.block_time is not None
    assert not failed.succeeded and failed.quality is DataQuality.PARTIAL


def test_wallet_provider_unexpected_payload(clock, sleeper):
    rpc = make_rpc(rpc_transport(lambda m, p: {"oops": 1}), clock, sleeper)
    res = run(RpcWalletActivityProvider(rpc).get_activity(WALLET, 10))
    assert res.items == [] and res.rejections
