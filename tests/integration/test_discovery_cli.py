"""End-to-end M2 flow through the CLI with mocked providers (no network)."""

import json
from datetime import timedelta

import httpx

from autotrade import cli
from autotrade.data.providers.dexscreener import DexScreenerProvider
from autotrade.data.providers.jupiter import JupiterProvider
from autotrade.domain.timeutil import utcnow
from autotrade.runtime import Providers
from tests.conftest import MINT, fixture, make_rest, make_rpc, mint_account, rpc_transport

DEX = fixture("dexscreener_tokens_wsol.json")[0]
JUP = fixture("jupiter_search_wsol.json")[0]
NEW = "DiKSyLHFrx819edefKGSqRksTFr9Uo6Rji8AdSmdpump"


def providers(sleeper, *, dex_ok=True, sells=500, mint_auth=None, m5=(5, 5)):
    now = utcnow

    def dex_handler(r: httpx.Request) -> httpx.Response:
        if not dex_ok:
            return httpx.Response(503)
        if r.url.path.startswith("/token-profiles"):
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": NEW}])
        mint = r.url.path.rsplit("/", 1)[1]
        pair = {
            **DEX,
            "baseToken": {**DEX["baseToken"], "address": mint},
            "txns": {
                "m5": {"buys": m5[0], "sells": m5[1]},
                "h1": {"buys": 50, "sells": 40},
                "h24": {"buys": 600, "sells": sells},
            },
        }
        return httpx.Response(200, json=[pair])

    def jup_handler(r: httpx.Request) -> httpx.Response:
        mint = r.url.params["query"]
        tok = {
            **JUP,
            "id": mint,
            "updatedAt": (now() - timedelta(seconds=2)).isoformat(),
            "usdPrice": float(DEX["priceUsd"]),
            "audit": {**JUP["audit"], "topHoldersPercentage": 18.5},
        }
        return httpx.Response(200, json=[tok])

    def rpc(method, params):
        return {"context": {"slot": 1}, "value": mint_account(decimals=9, mint_authority=mint_auth)}

    return Providers(
        rpc=make_rpc(rpc_transport(rpc), _Clock(), sleeper),
        dex=make_rest(DexScreenerProvider, dex_handler, _Clock(), sleeper, name="dexscreener"),
        jupiter=make_rest(JupiterProvider, jup_handler, _Clock(), sleeper, name="jupiter"),
    )


class _Clock:
    def __call__(self):
        return utcnow()


def env(tmp_path, **extra):
    return {
        "AUTOTRADE_DB_PATH": str(tmp_path / "d.db"),
        "AUTOTRADE_DISCOVERY_ENABLED": "true",
        "AUTOTRADE_MAX_SNAPSHOT_AGE_SECONDS": "60",
        **extra,
    }


def run_cli(monkeypatch, tmp_path, sleeper, argv, e=None, **kw):
    monkeypatch.setattr(cli, "build_providers", lambda config: providers(sleeper, **kw))
    return cli.main(argv, e or env(tmp_path))


def rows(tmp_path, sql):
    import sqlite3

    c = sqlite3.connect(tmp_path / "d.db")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_discovery_then_screening_pass_in_one_cycle(tmp_path, monkeypatch, capsys, sleeper):
    cli.main(["db", "init"], env(tmp_path))
    assert run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"]) == 0
    out = capsys.readouterr().out
    assert "discovery: success" in out and "token_metadata: success" in out
    assert f"safety {NEW[:8]}…: PASS" in out
    cand = rows(tmp_path, "SELECT * FROM candidates")
    assert [c["mint"] for c in cand] == [NEW]
    ev = rows(tmp_path, "SELECT * FROM safety_evaluations")[0]
    assert ev["verdict"] == "pass" and json.loads(ev["inputs"])["snapshot_ids"]


def test_discovered_token_with_live_mint_authority_is_rejected(
    tmp_path, monkeypatch, capsys, sleeper
):
    cli.main(["db", "init"], env(tmp_path))
    from tests.conftest import WALLET

    assert run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], mint_auth=WALLET) == 0
    out = capsys.readouterr().out
    assert "REJECT" in out and "mint_authority_revoked:fail" in out


def test_low_sell_activity_is_rejected(tmp_path, monkeypatch, capsys, sleeper):
    cli.main(["db", "init"], env(tmp_path))
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], sells=3)
    assert "sellability_evidence:fail" in capsys.readouterr().out


def test_dex_outage_means_reject_not_stale_pass(tmp_path, monkeypatch, capsys, sleeper):
    cli.main(["db", "init"], env(tmp_path))
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"], dex_ok=False)
    out = capsys.readouterr().out
    assert "discovery: failed" in out
    assert rows(tmp_path, "SELECT COUNT(*) n FROM candidates")[0]["n"] == 0
    assert rows(tmp_path, "SELECT COUNT(*) n FROM safety_evaluations")[0]["n"] == 0


def test_provider_outage_after_discovery_keeps_candidate_but_rejects(
    tmp_path, monkeypatch, capsys, sleeper
):
    cli.main(["db", "init"], env(tmp_path))
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"])
    capsys.readouterr()
    # Market data now unavailable: stored candidate remains, a new evaluation must not PASS
    monkeypatch.setattr(cli, "utcnow", lambda: utcnow() + timedelta(hours=1))
    assert cli.main(["safety", "-v"], env(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "REJECT" in out and "market_data_fresh: fail" in out


def test_candidates_and_safety_commands(tmp_path, monkeypatch, capsys, sleeper):
    e = env(tmp_path)
    cli.main(["db", "init"], e)
    assert cli.main(["candidates"], e) == 0
    assert "no candidates" in capsys.readouterr().out
    assert cli.main(["safety"], e) == 3  # nothing to evaluate
    run_cli(monkeypatch, tmp_path, sleeper, ["ingest", "--once"])
    capsys.readouterr()
    assert cli.main(["candidates"], e) == 0
    assert f"{NEW} " in capsys.readouterr().out
    assert cli.main(["safety", "--mint", NEW], e) == 0  # fresh data still within 60s
    assert cli.main(["safety", "--mint", MINT], e) == 1  # never ingested => reject


def test_discovery_is_opt_in(tmp_path, capsys):
    e = {"AUTOTRADE_DB_PATH": str(tmp_path / "d.db")}
    cli.main(["db", "init"], e)
    assert cli.main(["ingest", "--once"], e) == 3
    assert "nothing to ingest" in capsys.readouterr().out
