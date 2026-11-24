import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from autotrade.config import AppConfig
from autotrade.data.providers.birdeye import BirdeyeProvider
from autotrade.data.providers.dexscreener import DexScreenerProvider
from autotrade.data.providers.jupiter import JupiterProvider
from autotrade.data.providers.rpc_adapters import RpcTokenMetadataProvider
from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.data.storage.migrations import MIGRATIONS
from autotrade.domain.models import CheckStatus, RunStatus
from autotrade.ingestion.crosscheck import run_cross_checks
from autotrade.ingestion.engine import IngestionEngine
from autotrade.ingestion.jobs import MarketSnapshotJob, OhlcvJob, TokenIntelJob, TokenMetadataJob
from autotrade.runtime import Providers, build_jobs, build_providers
from tests.conftest import MINT, fixture, make_rest, make_rpc, mint_account, rpc_transport

run = asyncio.run
DEX = fixture("dexscreener_tokens_wsol.json")
JUP = fixture("jupiter_search_wsol.json")


@pytest.fixture
def now_clock(clock):
    clock.now = clock.now.replace(year=2027)  # after the recorded Jupiter updatedAt
    return clock


def engine(db, clock):
    return IngestionEngine(db, clock=clock)


def dex_p(clock, sleeper, handler=lambda r: httpx.Response(200, json=DEX)):
    return make_rest(DexScreenerProvider, handler, clock, sleeper, name="dexscreener")


def jup_p(clock, sleeper, tok=None, status=200):
    body = JUP if tok is None else [tok]
    return make_rest(
        JupiterProvider, lambda r: httpx.Response(status, json=body), clock, sleeper, name="jupiter"
    )


def test_snapshots_persist_as_history_and_dedupe(db, now_clock, sleeper):
    p = dex_p(now_clock, sleeper)
    job = lambda: [MarketSnapshotJob(p, [MINT], 30)]  # noqa: E731
    r1 = run(engine(db, now_clock).run_once(job()))[0]
    assert r1.status is RunStatus.SUCCESS and r1.inserted == 1
    r_same = run(engine(db, now_clock).run_once(job()))[0]  # same collection instant
    assert (r_same.inserted, r_same.duplicates) == (0, 1)
    now_clock.advance(60)
    r2 = run(engine(db, now_clock).run_once(job()))[0]  # new poll => new historical row
    assert r2.inserted == 1
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM market_snapshots ORDER BY observed_at").fetchall()
        assert len(rows) == 2 and rows[0]["price_usd"] == "70.43"
        assert (
            rows[0]["source"] == "dexscreener" and rows[0]["received_at"] < rows[1]["received_at"]
        )
        assert repo.table_counts(conn)["pools"] == 1 and db.integrity_check(conn).ok


def test_intel_persisted_with_provider_timestamp_and_dedupe(db, now_clock, sleeper):
    job = lambda: [TokenIntelJob(jup_p(now_clock, sleeper), [MINT])]  # noqa: E731
    assert run(engine(db, now_clock).run_once(job()))[0].inserted == 1
    again = run(engine(db, now_clock).run_once(job()))[0]
    assert again.inserted == 0 and again.duplicates == 1  # same provider updatedAt
    with db.session() as conn:
        row = conn.execute("SELECT * FROM token_intel").fetchone()
        assert row["mint_authority_disabled"] == 1 and row["observed_at"].startswith("2026-11-24")
        assert row["source"] == "jupiter" and row["usd_price"] == "70.48521953022977"


def test_ohlcv_persist_and_conflict(db, now_clock, sleeper):
    def handler(r):
        return httpx.Response(200, json=fixture("birdeye_ohlcv.json"))

    p = make_rest(BirdeyeProvider, handler, now_clock, sleeper, name="birdeye")
    job = lambda: [OhlcvJob(p, [MINT], "15m", 4, now_clock)]  # noqa: E731
    assert run(engine(db, now_clock).run_once(job()))[0].inserted == 2
    assert run(engine(db, now_clock).run_once(job()))[0].duplicates == 2
    with db.session() as conn:
        assert repo.table_counts(conn)["ohlcv_candles"] == 2
        assert (
            conn.execute("SELECT open FROM ohlcv_candles ORDER BY open_time").fetchone()[0]
            == "127.1"
        )


def _seed(db, clock, sleeper, *, dex_price="70.43", jup_tok=None, onchain=True):
    pair = {**DEX[0], "priceUsd": dex_price}
    jobs = [
        MarketSnapshotJob(
            dex_p(clock, sleeper, lambda r: httpx.Response(200, json=[pair])), [MINT], 30
        ),
        TokenIntelJob(jup_p(clock, sleeper, jup_tok), [MINT]),
    ]
    if onchain:
        rpc = make_rpc(
            rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": mint_account(decimals=9)}),
            clock,
            sleeper,
        )
        jobs.append(TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT]))
    return run(engine(db, clock).run_once(jobs))


def _checks(db, clock, tol="0.05"):
    with db.session() as conn, db.transaction(conn):
        out = run_cross_checks(
            conn, [MINT], clock.now, max_age_seconds=3e9, price_tolerance=Decimal(tol)
        )
    return {c.check_type: c for c in out}


def test_crosscheck_agreement(db, now_clock, sleeper):
    assert all(r.status is RunStatus.SUCCESS for r in _seed(db, now_clock, sleeper))
    c = _checks(db, now_clock)
    assert c["price_usd"].status is CheckStatus.OK and c["price_usd"].deviation < Decimal("0.01")
    assert {k: v.status for k, v in c.items() if k != "price_usd"} == {
        "decimals": CheckStatus.OK,
        "mint_authority": CheckStatus.OK,
        "freeze_authority": CheckStatus.OK,
    }
    assert c["mint_authority"].source_b == "solana_rpc.token"
    with db.session() as conn:
        assert repo.table_counts(conn)["cross_checks"] == 4


def test_crosscheck_flags_price_discrepancy(db, now_clock, sleeper):
    _seed(db, now_clock, sleeper, dex_price="100")
    c = _checks(db, now_clock)["price_usd"]
    assert c.status is CheckStatus.DISCREPANCY and c.deviation > Decimal("0.3")


def test_crosscheck_flags_onchain_mismatch(db, now_clock, sleeper):
    lying = {**JUP[0], "audit": {**JUP[0]["audit"], "mintAuthorityDisabled": False}}
    _seed(db, now_clock, sleeper, jup_tok=lying)
    c = _checks(db, now_clock)
    assert c["mint_authority"].status is CheckStatus.DISCREPANCY
    assert c["freeze_authority"].status is CheckStatus.OK


def test_crosscheck_unknown_inputs_are_unavailable_not_ok(db, now_clock, sleeper):
    c = _checks(db, now_clock)  # nothing ingested at all
    assert {v.status for v in c.values()} == {CheckStatus.UNAVAILABLE}
    _seed(db, now_clock, sleeper, onchain=False)
    c = _checks(db, now_clock)
    assert c["price_usd"].status is CheckStatus.OK
    assert c["mint_authority"].status is CheckStatus.UNAVAILABLE  # no on-chain record


def test_crosscheck_stale_inputs_unavailable(db, now_clock, sleeper):
    _seed(db, now_clock, sleeper)
    with db.session() as conn, db.transaction(conn):
        out = run_cross_checks(
            conn,
            [MINT],
            now_clock.now + timedelta(hours=2),
            max_age_seconds=60,
            price_tolerance=Decimal("0.05"),
        )
    assert {c.status for c in out} == {CheckStatus.UNAVAILABLE}


def test_one_provider_down_does_not_block_or_corrupt_others(db, now_clock, sleeper):
    down = jup_p(now_clock, sleeper, status=503)
    jobs = [TokenIntelJob(down, [MINT]), MarketSnapshotJob(dex_p(now_clock, sleeper), [MINT], 30)]
    res = run(engine(db, now_clock).run_once(jobs))
    assert [r.status for r in res] == [RunStatus.FAILED, RunStatus.SUCCESS]
    with db.session() as conn:
        counts = repo.table_counts(conn)
        assert counts["token_intel"] == 0 and counts["market_snapshots"] == 1
        health = {h["provider"]: h["status"] for h in repo.latest_health(conn)}
        assert health["jupiter"] != "healthy" and health["dexscreener"] == "healthy"


def test_failed_dex_poll_keeps_previous_history(db, now_clock, sleeper):
    run(engine(db, now_clock).run_once([MarketSnapshotJob(dex_p(now_clock, sleeper), [MINT], 30)]))
    now_clock.advance(60)
    bad = dex_p(now_clock, sleeper, lambda r: httpx.Response(200, json={"oops": 1}))
    res = run(engine(db, now_clock).run_once([MarketSnapshotJob(bad, [MINT], 30)]))[0]
    assert res.status is RunStatus.FAILED
    with db.session() as conn:
        assert repo.table_counts(conn)["market_snapshots"] == 1


def test_invalid_pair_never_persisted_but_valid_siblings_are(db, now_clock, sleeper):
    bad = {**DEX[0], "pairAddress": "badaddr", "priceNative": "0"}
    p = dex_p(now_clock, sleeper, lambda r: httpx.Response(200, json=[DEX[0], bad]))
    res = run(engine(db, now_clock).run_once([MarketSnapshotJob(p, [MINT], 30)]))[0]
    assert res.status is RunStatus.PARTIAL and res.inserted == 1 and res.rejected == 1


def test_stale_snapshot_flagged_when_received_long_after_observed(db, now_clock, sleeper):
    from autotrade.data.providers.base import FetchResult, MarketObservation
    from autotrade.domain.models import MarketSnapshot, Pool
    from tests.conftest import MINT2, WALLET

    class Old:
        name = "old"
        health = lambda self: dex_p(now_clock, sleeper).health()  # noqa: E731

        async def get_market(self, mint):
            kw = dict(
                source="old",
                observed_at=now_clock.now - timedelta(minutes=10),
                received_at=now_clock.now,
            )
            return FetchResult(
                [
                    MarketObservation(
                        Pool(address=WALLET, dex="d", base_mint=mint, quote_mint=MINT2, **kw),
                        MarketSnapshot(
                            pool_address=WALLET,
                            base_mint=mint,
                            quote_mint=MINT2,
                            price_quote="1",
                            **kw,
                        ),
                    )
                ],
                fetched=1,
            )

    res = run(engine(db, now_clock).run_once([MarketSnapshotJob(Old(), [MINT], 30)]))[0]
    assert res.stale == 1
    with db.session() as conn:
        assert conn.execute("SELECT quality FROM market_snapshots").fetchone()[0] == "stale"


# ---- wiring / config / migration ------------------------------------------------------
def test_build_providers_defaults_and_optional_birdeye():
    cfg = AppConfig.from_env({"AUTOTRADE_WATCH_MINTS": MINT})
    prov = build_providers(cfg)
    assert prov.dex and prov.jupiter and prov.birdeye is None
    assert [j.name for j in build_jobs(cfg, prov)] == [
        "token_metadata",
        "market_snapshots",
        "token_intel",
    ]
    cfg = AppConfig.from_env(
        {"AUTOTRADE_WATCH_MINTS": MINT, "BIRDEYE_API_KEY": "k", "AUTOTRADE_ENABLE_JUPITER": "false"}
    )
    prov = build_providers(cfg)
    assert prov.birdeye and prov.jupiter is None
    assert [j.name for j in build_jobs(cfg, prov)] == [
        "token_metadata",
        "market_snapshots",
        "ohlcv",
    ]


def test_providers_respect_documented_dexscreener_limit():
    with pytest.raises(ValidationError):
        AppConfig.from_env({"DEXSCREENER_RPS": "10"})  # docs: 300 req/min = 5 rps


def test_migration_v1_to_v2_preserves_data(tmp_path, clock):
    path = tmp_path / "up.db"
    old = Database(path, clock=clock, migrations=MIGRATIONS[:1])
    with old.session() as conn:
        old.migrate(conn)
        conn.execute(
            "INSERT INTO pools(address,dex,base_mint,quote_mint,first_seen_at,"
            "last_seen_at,source,observed_at,received_at,quality) VALUES "
            "('p','d','a','b','t','t','s','t','t','ok')"
        )
        conn.execute(
            "INSERT INTO market_snapshots(pool_address,base_mint,quote_mint,price_quote,"
            "source,observed_at,received_at,quality) VALUES ('p','a','b','1','s','t','t','ok')"
        )
    new = Database(path, clock=clock)
    with new.session() as conn:
        assert new.migrate(conn) == [2, 3, 4, 5, 6, 7, 8]
        row = conn.execute("SELECT price_quote, fdv_usd, buys_1h FROM market_snapshots").fetchone()
        assert tuple(row) == ("1", None, None) and new.integrity_check(conn).ok
        assert new.schema_version(conn) == 8


def test_providers_container_closes_all(clock, sleeper):
    prov = Providers(
        rpc=make_rpc(rpc_transport(lambda m, p: 1), clock, sleeper), dex=dex_p(clock, sleeper)
    )
    assert len(prov.all()) == 2
    run(prov.aclose())


def test_schema_blocks_bad_intel_rows(db):
    with db.session() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cross_checks(check_type,mint,status,checked_at) "
            "VALUES ('x','m','maybe','t')"
        )
