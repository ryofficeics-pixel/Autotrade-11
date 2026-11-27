import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from autotrade.config import AppConfig, SafetyPolicy
from autotrade.data.providers.base import DiscoveryProvider
from autotrade.data.providers.dexscreener import DexScreenerProvider
from autotrade.data.storage import repositories as repo
from autotrade.domain.errors import MalformedResponse, RateLimited
from autotrade.domain.models import (
    CheckStatus,
    CrossCheck,
    MarketSnapshot,
    Pool,
    RuleStatus,
    Token,
    TokenExtension,
    TokenIntel,
    Verdict,
)
from autotrade.safety.screening import RULESET_VERSION, evaluate_and_store, evaluate_mint
from tests.conftest import MINT, MINT2, WALLET, fixture, make_rest

run = asyncio.run
CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
POLICY = SafetyPolicy()


def seed(db, clock, **o):
    """Insert a fully passing data set; overrides tweak individual inputs."""
    now = clock.now
    kw = dict(source="s", observed_at=now, received_at=now)
    with db.session() as conn, db.transaction(conn):
        if o.get("token", True):
            repo.upsert_token(
                conn,
                Token(
                    mint=MINT,
                    decimals=9,
                    authorities_known=True,
                    token_program=o.get("program", CLASSIC),
                    mint_authority=o.get("mint_auth"),
                    freeze_authority=o.get("freeze_auth"),
                    extensions=o.get("ext", () if o.get("program", CLASSIC) == CLASSIC else None),
                    **{**kw, "source": "solana_rpc.token"},
                ),
            )
        pool = Pool(
            address=WALLET,
            dex="d",
            base_mint=MINT,
            quote_mint=MINT2,
            created_at=o.get("created", now - timedelta(hours=2)),
            **{**kw, "source": "dexscreener"},
        )
        repo.upsert_pool(conn, pool)
        if o.get("snapshot", True):
            liq = o.get("liq", "50000")
            repo.insert_snapshot(
                conn,
                MarketSnapshot(
                    pool_address=WALLET,
                    base_mint=MINT,
                    quote_mint=MINT2,
                    price_quote="1",
                    price_usd="2",
                    liquidity_usd=liq,
                    buys_24h=o.get("buys24", 600),
                    sells_24h=o.get("sells24", 500),
                    sells_1h=o.get("sells1", 40),
                    observed_at=o.get("snap_at", now),
                    received_at=now,
                    source="dexscreener",
                    quality="ok" if liq else "partial",
                    quality_notes=() if liq else ("liquidity not reported",),
                ),
            )
        if o.get("intel", True):
            repo.insert_intel(
                conn,
                TokenIntel(
                    mint=MINT,
                    top_holders_pct=o.get("holders", "20"),
                    usd_price="2",
                    **{**kw, "source": "jupiter"},
                ),
            )
        status = o.get("price_check", CheckStatus.OK)
        if status is not None:
            repo.insert_cross_check(
                conn,
                CrossCheck(
                    check_type="price_usd",
                    mint=MINT,
                    status=status,
                    checked_at=now,
                    deviation=Decimal("0.001"),
                ),
            )
        if o.get("bad_decimals"):
            repo.insert_cross_check(
                conn,
                CrossCheck(
                    check_type="decimals", mint=MINT, status=CheckStatus.DISCREPANCY, checked_at=now
                ),
            )


def ev(db, clock, policy=POLICY):
    with db.session() as conn:
        return evaluate_mint(conn, MINT, clock.now, policy, max_data_age_seconds=30)


def by_rule(e):
    return {r.rule: r.status for r in e.rules}


def test_all_rules_pass_gives_pass_with_full_evidence(db, clock):
    seed(db, clock)
    e = ev(db, clock)
    assert e.verdict is Verdict.PASS and e.reasons == () and e.ruleset_version == RULESET_VERSION
    assert {r.rule for r in e.rules} >= {
        "onchain_record",
        "token_program",
        "token_extensions",
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "market_data_fresh",
        "liquidity",
        "pool_age",
        "holder_concentration",
        "sellability_evidence",
        "price_cross_check",
        "provider_vs_onchain",
    }
    assert all(r.status is RuleStatus.PASS for r in e.rules)
    assert (
        e.inputs["snapshot_ids"]
        and e.inputs["intel_id"]
        and "price_usd" in e.inputs["cross_check_ids"]
    )


@pytest.mark.parametrize(
    "overrides,rule,status",
    [
        ({"mint_auth": WALLET}, "mint_authority_revoked", RuleStatus.FAIL),
        ({"freeze_auth": WALLET}, "freeze_authority_revoked", RuleStatus.FAIL),
        ({"program": TOKEN_2022}, "token_extensions", RuleStatus.UNKNOWN),  # not analysed
        (
            {"program": "1" * 32},  # unrecognised program
            "token_program",
            RuleStatus.FAIL,
        ),
        ({"liq": "9999"}, "liquidity", RuleStatus.FAIL),
        ({"liq": None}, "liquidity", RuleStatus.UNKNOWN),
        ({"created": None}, "pool_age", RuleStatus.UNKNOWN),
        ({"holders": "41"}, "holder_concentration", RuleStatus.FAIL),
        ({"intel": False}, "holder_concentration", RuleStatus.UNKNOWN),
        ({"sells24": 5}, "sellability_evidence", RuleStatus.FAIL),
        ({"sells1": 0}, "sellability_evidence", RuleStatus.FAIL),
        ({"sells24": 50, "buys24": 5000}, "sellability_evidence", RuleStatus.FAIL),
        ({"price_check": CheckStatus.DISCREPANCY}, "price_cross_check", RuleStatus.FAIL),
        ({"price_check": CheckStatus.UNAVAILABLE}, "price_cross_check", RuleStatus.UNKNOWN),
        ({"price_check": None}, "price_cross_check", RuleStatus.UNKNOWN),
        ({"bad_decimals": True}, "provider_vs_onchain", RuleStatus.FAIL),
        ({"token": False}, "onchain_record", RuleStatus.UNKNOWN),
        ({"snapshot": False}, "market_data_fresh", RuleStatus.UNKNOWN),
    ],
)
def test_each_rule_rejects(db, clock, overrides, rule, status):
    seed(db, clock, **overrides)
    e = ev(db, clock)
    assert by_rule(e)[rule] is status
    assert e.verdict is Verdict.REJECT and f"{rule}:{status.value}" in e.reasons


def test_pool_too_young_is_rejected(db, clock):
    seed(db, clock, created=clock.now - timedelta(minutes=5))
    assert by_rule(ev(db, clock))["pool_age"] is RuleStatus.FAIL


def test_stale_market_data_is_rejected_not_used(db, clock):
    seed(db, clock)
    clock.advance(120)
    e = ev(db, clock)
    assert e.verdict is Verdict.REJECT
    assert by_rule(e)["market_data_fresh"] is RuleStatus.FAIL
    assert by_rule(e)["liquidity"] is RuleStatus.UNKNOWN  # stale value never trusted
    assert by_rule(e)["holder_concentration"] is RuleStatus.UNKNOWN


def test_no_data_at_all_rejects_everything(db, clock):
    e = ev(db, clock)
    assert e.verdict is Verdict.REJECT and all(r.status is not RuleStatus.PASS for r in e.rules[:1])
    assert RuleStatus.PASS not in {r.status for r in e.rules if r.rule != "provider_vs_onchain"}


def test_policy_can_be_configured_and_is_recorded(db, clock):
    seed(db, clock, liq="9999")
    lax = SafetyPolicy(min_liquidity_usd=Decimal("5000"))
    e = ev(db, clock, lax)
    assert by_rule(e)["liquidity"] is RuleStatus.PASS
    assert e.inputs["policy"]["min_liquidity_usd"] == "5000"


def ext(name, **detail):
    return TokenExtension(name=name, detail=detail)


def test_token_2022_with_benign_extensions_passes(db, clock):
    seed(db, clock, program=TOKEN_2022, ext=(ext("metadataPointer"), ext("tokenMetadata")))
    e = ev(db, clock)
    assert e.verdict is Verdict.PASS
    assert by_rule(e)["token_program"] is RuleStatus.PASS
    assert by_rule(e)["token_extensions"] is RuleStatus.PASS


def test_token_2022_without_extensions_passes(db, clock):
    seed(db, clock, program=TOKEN_2022, ext=())
    assert ev(db, clock).verdict is Verdict.PASS


def test_token_2022_can_be_disabled_by_policy(db, clock):
    seed(db, clock, program=TOKEN_2022, ext=())
    e = ev(db, clock, SafetyPolicy(allow_token_2022=False))
    assert by_rule(e)["token_program"] is RuleStatus.FAIL and e.verdict is Verdict.REJECT


@pytest.mark.parametrize(
    "extension,status",
    [
        (ext("permanentDelegate", delegate=WALLET), RuleStatus.FAIL),
        (ext("permanentDelegate", delegate=None), RuleStatus.PASS),
        (ext("transferHook", program_id=WALLET, authority=None), RuleStatus.FAIL),
        (ext("transferHook", program_id=None, authority=None), RuleStatus.PASS),
        (ext("nonTransferable"), RuleStatus.FAIL),
        (ext("confidentialTransferMint"), RuleStatus.FAIL),
        (ext("defaultAccountState", state="frozen"), RuleStatus.FAIL),
        (ext("defaultAccountState", state="initialized"), RuleStatus.PASS),
        (ext("pausableConfig", authority=WALLET), RuleStatus.FAIL),
        (ext("transferFeeConfig", fee_bps="100", config_authority=None), RuleStatus.PASS),
        (ext("transferFeeConfig", fee_bps="101", config_authority=None), RuleStatus.FAIL),
        (ext("transferFeeConfig", fee_bps="50", config_authority=WALLET), RuleStatus.FAIL),
        (ext("transferFeeConfig", fee_bps=None, config_authority=None), RuleStatus.UNKNOWN),
        (ext("someFutureExtension"), RuleStatus.UNKNOWN),
        (ext("unparseableExtension"), RuleStatus.UNKNOWN),
    ],
)
def test_dangerous_extensions_reject_unknown_ones_are_unknown(db, clock, extension, status):
    seed(db, clock, program=TOKEN_2022, ext=(ext("metadataPointer"), extension))
    e = ev(db, clock)
    assert by_rule(e)["token_extensions"] is status
    assert (e.verdict is Verdict.PASS) == (status is RuleStatus.PASS)


def test_one_bad_extension_among_benign_ones_rejects_with_reason(db, clock):
    seed(
        db,
        clock,
        program=TOKEN_2022,
        ext=(ext("tokenMetadata"), ext("permanentDelegate", delegate=WALLET)),
    )
    r = next(r for r in ev(db, clock).rules if r.rule == "token_extensions")
    assert r.status is RuleStatus.FAIL and "permanentDelegate" in (r.detail or "")


def test_classic_token_has_no_extension_risk(db, clock):
    r = next(r for r in (ev_seeded(db, clock)).rules if r.rule == "token_extensions")
    assert r.status is RuleStatus.PASS and "classic" in r.observed


def ev_seeded(db, clock):
    seed(db, clock)
    return ev(db, clock)


def test_evaluations_persist_append_only_and_traceable(db, clock):
    seed(db, clock)
    with db.session() as conn, db.transaction(conn):
        evaluate_and_store(conn, [MINT], clock.now, POLICY, max_data_age_seconds=30)
        evaluate_and_store(conn, [MINT], clock.now, POLICY, max_data_age_seconds=30)
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM safety_evaluations").fetchall()
        assert len(rows) == 2 and rows[0]["verdict"] == "pass" and rows[0]["ruleset_version"]
        assert "snapshot_ids" in rows[0]["inputs"] and "liquidity" in rows[0]["rules"]
        assert repo.latest_evaluation(conn, MINT)["id"] == rows[1]["id"]


def test_no_live_execution_tables_exist(db):
    with db.session() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    banned = ("live", "transaction", "signed", "keypair", "wallet_key")
    assert not {t for t in tables if any(w in t for w in banned)}


# ---- discovery -----------------------------------------------------------------------
def prof(handler, clock, sleeper):
    return make_rest(DexScreenerProvider, handler, clock, sleeper, name="dexscreener")


def test_discovery_parses_recorded_profiles_solana_only(clock, sleeper):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json=fixture("dexscreener_profiles.json"))

    p = prof(handler, clock, sleeper)
    assert isinstance(p, DiscoveryProvider)
    res = run(p.discover())
    assert seen[0].url.path == "/token-profiles/latest/v1"
    assert len(res.items) == 3 and res.fetched == 4  # non-solana profile dropped
    assert res.items[0].feed == "token-profiles" and res.items[0].observed_at == clock.now


def test_discovery_rejects_bad_rows_and_bad_shapes(clock, sleeper):
    rows = [
        {"chainId": "solana", "tokenAddress": "nope"},
        {"chainId": "solana"},
        {"chainId": "solana", "tokenAddress": MINT},
    ]
    res = run(prof(lambda r: httpx.Response(200, json=rows), clock, sleeper).discover())
    assert [t.mint for t in res.items] == [MINT] and len(res.rejections) == 2
    with pytest.raises(MalformedResponse):
        run(prof(lambda r: httpx.Response(200, json={"x": 1}), clock, sleeper).discover())
    with pytest.raises(RateLimited):
        run(prof(lambda r: httpx.Response(429), clock, sleeper).discover())


def test_safety_config_from_env_and_bounds():
    c = AppConfig.from_env(
        {
            "AUTOTRADE_SAFETY_MIN_LIQUIDITY_USD": "25000",
            "AUTOTRADE_DISCOVERY_ENABLED": "true",
            "AUTOTRADE_SAFETY_ALLOW_TOKEN_2022": "false",
        }
    )
    assert c.safety.min_liquidity_usd == Decimal("25000") and c.data.discovery_enabled
    assert c.safety.allow_token_2022 is False
    assert (
        AppConfig().data.discovery_enabled is False and AppConfig().safety.allow_token_2022 is True
    )
    with pytest.raises(ValueError):
        AppConfig.from_env({"AUTOTRADE_SAFETY_MAX_TOP_HOLDERS_PCT": "0"})
