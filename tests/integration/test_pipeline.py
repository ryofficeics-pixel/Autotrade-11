import asyncio
import sqlite3
from datetime import timedelta

import httpx

from autotrade.data.providers.base import DexMarketDataProvider, FetchResult, MarketObservation
from autotrade.data.providers.rpc_adapters import (
    RpcTokenMetadataProvider,
    RpcWalletActivityProvider,
)
from autotrade.data.storage import repositories as repo
from autotrade.domain.models import (
    HealthStatus,
    MarketSnapshot,
    Pool,
    ProviderHealth,
    RunStatus,
)
from autotrade.ingestion.engine import IngestionEngine
from autotrade.ingestion.jobs import MarketSnapshotJob, TokenMetadataJob, WalletActivityJob
from autotrade.ingestion.scheduler import poll
from tests.conftest import MINT, MINT2, WALLET, WALLET2, make_rpc, mint_account, rpc_transport, sig

run = asyncio.run
CTX = lambda v: {"context": {"slot": 1}, "value": v}  # noqa: E731


def rpc_handler(fail_mints=(), calls=None):
    def handler(method, params):
        if method == "getAccountInfo":
            if params[0] in fail_mints:
                return httpx.Response(503)
            return CTX(mint_account(decimals=6 if params[0] == MINT else 9))
        if method == "getSignaturesForAddress":
            bt = 1767268800
            return [
                {"signature": sig(i), "slot": 100 + i, "err": None, "blockTime": bt + i}
                for i in range(3)
            ]
        raise AssertionError(method)

    return handler


def engine(db, clock):
    return IngestionEngine(db, clock=clock, max_concurrency=2)


def test_full_pipeline_tokens_and_wallets(db, clock, sleeper):
    rpc = make_rpc(rpc_transport(rpc_handler()), clock, sleeper)
    jobs = [
        TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT, MINT2]),
        WalletActivityJob(RpcWalletActivityProvider(rpc), [WALLET, WALLET2], 10, clock),
    ]
    results = run(engine(db, clock).run_once(jobs))
    assert [r.status for r in results] == [RunStatus.SUCCESS, RunStatus.SUCCESS]
    assert (results[0].inserted, results[1].inserted) == (2, 6)
    with db.session() as conn:
        counts = repo.table_counts(conn)
        assert counts["tokens"] == 2 and counts["wallets"] == 2 and counts["wallet_activity"] == 6
        assert counts["ingestion_runs"] == 2 and counts["provider_health"] == 2
        runs = repo.recent_runs(conn, 5)
        assert all(r["finished_at"] and r["status"] == "success" for r in runs)
        assert repo.latest_health(conn)[0]["status"] == "healthy"
        assert db.integrity_check(conn).ok


def test_reingest_is_idempotent(db, clock, sleeper):
    rpc = make_rpc(rpc_transport(rpc_handler()), clock, sleeper)

    def jobs():
        return [WalletActivityJob(RpcWalletActivityProvider(rpc), [WALLET], 10, clock)]

    first = run(engine(db, clock).run_once(jobs()))[0]
    clock.advance(10)
    second = run(engine(db, clock).run_once(jobs()))[0]
    assert (first.inserted, second.inserted, second.duplicates) == (3, 0, 3)
    with db.session() as conn:
        assert repo.table_counts(conn)["wallet_activity"] == 3


def test_provider_failure_blocks_ingestion_and_preserves_data(db, clock, sleeper):
    good = make_rpc(rpc_transport(rpc_handler()), clock, sleeper)
    run(engine(db, clock).run_once([TokenMetadataJob(RpcTokenMetadataProvider(good), [MINT])]))
    with db.session() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM tokens")]
    clock.advance(60)
    down = make_rpc(rpc_transport(rpc_handler(fail_mints=(MINT,))), clock, sleeper, attempts=2)
    res = run(
        engine(db, clock).run_once([TokenMetadataJob(RpcTokenMetadataProvider(down), [MINT])])
    )[0]
    assert res.status is RunStatus.FAILED and res.accepted == 0 and res.error_kind == "unavailable"
    with db.session() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM tokens")] == before
        assert repo.recent_runs(conn, 1)[0]["status"] == "failed"
        assert repo.latest_health(conn)[0]["status"] in ("degraded", "down")


def test_partial_success_persists_only_valid_targets(db, clock, sleeper):
    rpc = make_rpc(rpc_transport(rpc_handler(fail_mints=(MINT2,))), clock, sleeper, attempts=1)
    res = run(
        engine(db, clock).run_once([TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT, MINT2])])
    )[0]
    assert res.status is RunStatus.PARTIAL and res.inserted == 1
    with db.session() as conn:
        assert [r["mint"] for r in conn.execute("SELECT mint FROM tokens")] == [MINT]


def test_recovery_after_temporary_outage(db, clock, sleeper):
    state = {"down": True}

    def handler(m, p):
        return httpx.Response(503) if state["down"] else CTX(mint_account())

    rpc = make_rpc(rpc_transport(handler), clock, sleeper, attempts=1)
    job = lambda: [TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT])]  # noqa: E731
    assert run(engine(db, clock).run_once(job()))[0].status is RunStatus.FAILED
    state["down"] = False
    clock.advance(30)
    assert run(engine(db, clock).run_once(job()))[0].status is RunStatus.SUCCESS
    assert rpc.health().status is HealthStatus.HEALTHY


def test_persist_failure_rolls_back_whole_batch(db, clock, sleeper):
    rpc = make_rpc(rpc_transport(rpc_handler()), clock, sleeper)
    job = WalletActivityJob(RpcWalletActivityProvider(rpc), [WALLET], 10, clock)
    with db.session() as conn:  # sabotage: a trigger that fails the 3rd activity insert
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON wallet_activity WHEN NEW.slot = 102 "
            "BEGIN SELECT RAISE(ABORT, 'disk on fire'); END"
        )
    res = run(engine(db, clock).run_once([job]))[0]
    assert res.status is RunStatus.FAILED and res.error_kind == "database"
    with db.session() as conn:
        counts = repo.table_counts(conn)
        assert counts["wallet_activity"] == 0 and counts["wallets"] == 0  # nothing partial
        assert db.integrity_check(conn).ok


def test_malformed_items_rejected_not_stored(db, clock, sleeper):
    def handler(m, p):
        return CTX({"data": {"program": "system"}, "owner": "x"})

    rpc = make_rpc(rpc_transport(handler), clock, sleeper)
    res = run(
        engine(db, clock).run_once([TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT])])
    )[0]
    assert res.status is RunStatus.FAILED and res.rejected == 1 and res.rejections
    with db.session() as conn:
        assert repo.table_counts(conn)["tokens"] == 0


class FakeDex:
    name = "fake_dex"

    def __init__(self, clock, age_seconds=1, price="1.5"):
        self.clock, self.age, self.price = clock, age_seconds, price

    def health(self):
        return ProviderHealth(
            provider=self.name, status=HealthStatus.HEALTHY, checked_at=self.clock()
        )

    async def get_market(self, mint):
        obs_at = self.clock() - timedelta(seconds=self.age)
        kw = dict(source=self.name, observed_at=obs_at, received_at=self.clock())
        pool = Pool(address=WALLET, dex="x", base_mint=mint, quote_mint=MINT2, **kw)
        snap = MarketSnapshot(
            pool_address=WALLET, base_mint=mint, quote_mint=MINT2, price_quote=self.price, **kw
        )
        return FetchResult([MarketObservation(pool, snap)], fetched=1)


def test_fake_dex_satisfies_contract():
    assert isinstance(FakeDex(None), DexMarketDataProvider)


def test_market_snapshots_idempotent_and_stale_flagged(db, clock):
    dex = FakeDex(clock, age_seconds=1)
    job = lambda d: [MarketSnapshotJob(d, [MINT], 30)]  # noqa: E731
    first = run(engine(db, clock).run_once(job(dex)))[0]
    again = run(engine(db, clock).run_once(job(dex)))[0]
    assert (first.inserted, again.inserted, again.duplicates) == (1, 0, 1)
    old = run(engine(db, clock).run_once(job(FakeDex(clock, age_seconds=500))))[0]
    assert old.stale == 1 and old.inserted == 1
    with db.session() as conn:
        qualities = [
            r["quality"] for r in conn.execute("SELECT quality FROM market_snapshots ORDER BY id")
        ]
        assert qualities == ["ok", "stale"]
        notes = conn.execute(
            "SELECT quality_notes FROM market_snapshots WHERE quality='stale'"
        ).fetchone()[0]
        assert "freshness limit" in notes


def test_contradictory_snapshot_in_db_is_rejected(db, clock):
    run(engine(db, clock).run_once([MarketSnapshotJob(FakeDex(clock, 1, "1.5"), [MINT], 30)]))
    res = run(
        engine(db, clock).run_once([MarketSnapshotJob(FakeDex(clock, 1, "2.5"), [MINT], 30)])
    )[0]
    assert res.status is RunStatus.FAILED and res.inserted == 0
    with db.session() as conn:
        assert conn.execute("SELECT price_quote FROM market_snapshots").fetchone()[0] == "1.5"


def test_polling_is_bounded_and_stops_gracefully(db, clock, sleeper):
    rpc = make_rpc(rpc_transport(rpc_handler()), clock, sleeper)
    job = TokenMetadataJob(RpcTokenMetadataProvider(rpc), [MINT])

    async def go():
        stop = asyncio.Event()
        out = await poll(engine(db, clock), [job], 0.01, stop, max_cycles=3)
        stop2 = asyncio.Event()
        stop2.set()
        none = await poll(engine(db, clock), [job], 0.01, stop2)
        return out, none

    cycles, nothing = run(go())
    assert len(cycles) == 3 and nothing == []
    with db.session() as conn:
        assert repo.table_counts(conn)["ingestion_runs"] == 3


def test_sqlite_error_type_is_database_error():
    assert issubclass(sqlite3.IntegrityError, sqlite3.Error)
