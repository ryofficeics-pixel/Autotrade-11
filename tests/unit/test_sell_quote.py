import asyncio
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from autotrade.data.providers.base import SellQuoteProvider
from autotrade.data.providers.jupiter import JupiterProvider
from autotrade.data.storage import repositories as repo
from autotrade.domain.errors import HttpStatusError, InvalidRequest, RateLimited
from autotrade.domain.models import MarketSnapshot, Pool, SellQuote, Token
from autotrade.ingestion.engine import IngestionEngine
from autotrade.ingestion.jobs import SellQuoteJob
from tests.conftest import MINT, MINT2, WALLET, fixture, make_rest

run = asyncio.run
QUOTE = fixture("jupiter_quote_sell.json")
PUMP = "7fmD1p3jzva28EPP9bPqbDoxZGj5j398yJoE3EVypump"


def jup(handler, clock, sleeper):
    return make_rest(JupiterProvider, handler, clock, sleeper, name="jupiter")


def test_contract_and_request_shape(clock, sleeper):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json=QUOTE)

    p = jup(handler, clock, sleeper)
    assert isinstance(p, SellQuoteProvider)
    res = run(p.get_sell_quote(PUMP, 100_000_000))
    r = seen[0]
    assert r.url.path == "/swap/v1/quote" and r.method == "GET"
    assert dict(r.url.params) == {
        "inputMint": PUMP,
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount": "100000000",
        "slippageBps": "100",
    }
    q = res.items[0]
    assert q.routable and q.out_amount_raw == 30087 and q.route_labels == ("Pump.fun Amm",)
    assert q.in_amount_raw == 100_000_000 and q.price_impact_pct == Decimal(QUOTE["priceImpactPct"])


def test_no_route_is_a_valid_negative_result_not_an_error(clock, sleeper):
    body = {"error": "The token x is not tradable", "errorCode": "TOKEN_NOT_TRADABLE"}
    res = run(jup(lambda r: httpx.Response(400, json=body), clock, sleeper).get_sell_quote(MINT, 5))
    q = res.items[0]
    assert not q.routable and q.error_code == "TOKEN_NOT_TRADABLE" and q.out_amount_raw is None


def test_other_failures_still_raise(clock, sleeper):
    other = {"error": "bad", "errorCode": "SOMETHING_ELSE"}
    with pytest.raises(HttpStatusError) as ei:
        run(jup(lambda r: httpx.Response(400, json=other), clock, sleeper).get_sell_quote(MINT, 5))
    assert ei.value.body == other
    with pytest.raises(HttpStatusError):
        run(jup(lambda r: httpx.Response(401), clock, sleeper).get_sell_quote(MINT, 5))
    with pytest.raises(RateLimited):
        run(jup(lambda r: httpx.Response(429), clock, sleeper).get_sell_quote(MINT, 5))


@pytest.mark.parametrize("patch", [{"routePlan": []}, {"outAmount": "0"}])
def test_empty_route_or_zero_output_is_unroutable(clock, sleeper, patch):
    body = {**QUOTE, **patch}
    p = jup(lambda r: httpx.Response(200, json=body), clock, sleeper)
    q = run(p.get_sell_quote(PUMP, 100_000_000)).items[0]
    assert not q.routable and q.error_code == "EMPTY_ROUTE"


def test_malformed_quote_is_rejected_and_bad_input_never_hits_network(clock, sleeper):
    res = run(
        jup(lambda r: httpx.Response(200, json={"x": 1}), clock, sleeper).get_sell_quote(MINT, 5)
    )
    assert res.items == [] and res.rejections
    called = []
    p = jup(lambda r: called.append(1) or httpx.Response(200, json=QUOTE), clock, sleeper)
    for args in (("bad", 5), (MINT, 0), (MINT, -1)):
        with pytest.raises(InvalidRequest):
            run(p.get_sell_quote(*args))
    assert called == []


def test_model_invariants(clock):
    kw = dict(source="s", observed_at=clock.now, received_at=clock.now, mint=MINT, in_amount_raw=1)
    with pytest.raises(ValidationError):
        SellQuote(routable=True, **kw)  # routable but no output/route
    with pytest.raises(ValidationError):
        SellQuote(routable=False, out_amount_raw=5, **kw)


def test_probe_sizing_from_price_and_decimals(db, clock):
    with db.session() as conn, db.transaction(conn):
        repo.upsert_token(
            conn,
            Token(
                source="s",
                observed_at=clock.now,
                received_at=clock.now,
                mint=MINT,
                decimals=6,
                authorities_known=True,
            ),
        )
        repo.upsert_pool(
            conn,
            Pool(
                address=WALLET,
                dex="d",
                base_mint=MINT,
                quote_mint=MINT2,
                source="dexscreener",
                observed_at=clock.now,
                received_at=clock.now,
            ),
        )
        repo.insert_snapshot(
            conn,
            MarketSnapshot(
                pool_address=WALLET,
                base_mint=MINT,
                quote_mint=MINT2,
                price_quote="1",
                price_usd="0.5",
                source="dexscreener",
                observed_at=clock.now,
                received_at=clock.now,
            ),
        )
        probes = repo.sell_probes(conn, [MINT, MINT2], Decimal(5))
    assert probes == [(MINT, 10_000_000)]  # $5 / $0.5 = 10 tokens = 10e6 raw; MINT2 has no data


def test_job_persists_idempotently_and_failure_is_isolated(db, clock, sleeper):
    p = jup(lambda r: httpx.Response(200, json=QUOTE), clock, sleeper)
    job = SellQuoteJob(p, lambda: [(PUMP, 100_000_000)])
    eng = IngestionEngine(db, clock=clock)
    first = run(eng.run_once([job]))[0]
    assert first.inserted == 1
    again = run(eng.run_once([job]))[0]  # same instant => same unique key
    assert (again.inserted, again.duplicates) == (0, 1)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM sell_quotes").fetchone()
    assert row["routable"] == 1 and row["route_labels"] == '["Pump.fun Amm"]'
    down = jup(lambda r: httpx.Response(503), clock, sleeper)
    res = run(IngestionEngine(db, clock=clock).run_once([SellQuoteJob(down, lambda: [(PUMP, 1)])]))[
        0
    ]
    assert res.status.value == "failed"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sell_quotes").fetchone()[0] == 1
