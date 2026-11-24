import asyncio
import copy
from datetime import timedelta
from decimal import Decimal

import pytest

from autotrade.config import AppConfig, SignalPolicy
from autotrade.data.providers.rpc_adapters import RpcWalletFlowProvider
from autotrade.data.providers.tx_parse import NATIVE, USDC, WSOL, parse_wallet_flows
from autotrade.data.storage import repositories as repo
from autotrade.domain.models import (
    FlowSide,
    MarketSnapshot,
    ParsedTransaction,
    Pool,
    Signal,
    Token,
    Verdict,
    Wallet,
)
from autotrade.domain.timeutil import from_unix
from autotrade.features.momentum import compute_features
from autotrade.safety.screening import evaluate_and_store
from autotrade.strategies.hybrid import STRATEGY_VERSION, generate_signal
from tests.conftest import MINT, MINT2, T0, WALLET, WALLET2, fixture, make_rpc, rpc_transport, sig

run = asyncio.run
SWAP = fixture("solana_swap_tx.json")
SWAP_WALLET = SWAP["transaction"]["message"]["accountKeys"][0]
SWAP_SIG = SWAP["_signature"]
PUMP = "7fmD1p3jzva28EPP9bPqbDoxZGj5j398yJoE3EVypump"


# ---- transaction parsing (real recorded mainnet swap) ------------------------------------
def test_real_mainnet_buy_is_parsed_as_swap():
    kind, legs = parse_wallet_flows(SWAP, SWAP_WALLET)
    assert kind == "swap" and len(legs) == 1
    leg = legs[0]
    assert (leg.mint, leg.side, leg.decimals) == (PUMP, FlowSide.BUY, 6)
    assert leg.token_delta_raw == 3074737349 and leg.counter == WSOL
    assert leg.counter_delta_raw == -100442


def test_sell_direction_is_derived_from_balance_change():
    tx = copy.deepcopy(SWAP)
    meta = tx["meta"]
    meta["preTokenBalances"], meta["postTokenBalances"] = (
        meta["postTokenBalances"],
        meta["preTokenBalances"],
    )
    kind, legs = parse_wallet_flows(tx, SWAP_WALLET)
    assert kind == "swap" and legs[0].side is FlowSide.SELL and legs[0].counter_delta_raw == 100442


def test_other_wallets_balances_are_ignored():
    kind, legs = parse_wallet_flows(SWAP, WALLET2)
    assert (kind, legs) == ("transfer", [])


def test_failed_and_missing_meta():
    tx = copy.deepcopy(SWAP)
    tx["meta"]["err"] = {"InstructionError": [0, "x"]}
    assert parse_wallet_flows(tx, SWAP_WALLET) == ("failed", [])
    tx["meta"] = None
    assert parse_wallet_flows(tx, SWAP_WALLET) == ("unparsed", [])
    assert parse_wallet_flows({}, SWAP_WALLET) == ("unparsed", [])


def test_token_moved_without_payment_is_transfer_not_buy():
    tx = copy.deepcopy(SWAP)
    tx["meta"]["preTokenBalances"] = [
        b for b in tx["meta"]["preTokenBalances"] if b["mint"] != WSOL
    ]
    tx["meta"]["postTokenBalances"] = [
        b for b in tx["meta"]["postTokenBalances"] if b["mint"] != WSOL
    ]
    assert parse_wallet_flows(tx, SWAP_WALLET) == ("transfer", [])


def test_native_sol_counter_leg_and_fee_adjustment():
    tx = copy.deepcopy(SWAP)
    tx["meta"]["preTokenBalances"] = [
        b for b in tx["meta"]["preTokenBalances"] if b["mint"] != WSOL
    ]
    tx["meta"]["postTokenBalances"] = [
        b for b in tx["meta"]["postTokenBalances"] if b["mint"] != WSOL
    ]
    tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - 50_000_000 - tx["meta"]["fee"]
    kind, legs = parse_wallet_flows(tx, SWAP_WALLET)
    assert kind == "swap" and legs[0].counter == NATIVE and legs[0].counter_delta_raw == -50_000_000


def test_sub_dust_native_change_is_not_a_payment():
    tx = copy.deepcopy(SWAP)
    for key in ("preTokenBalances", "postTokenBalances"):
        tx["meta"][key] = [b for b in tx["meta"][key] if b["mint"] != WSOL]
    meta = tx["meta"]
    meta["postBalances"][0] = meta["preBalances"][0] - meta["fee"] - 500  # 500 lamports of noise
    assert parse_wallet_flows(tx, SWAP_WALLET) == ("transfer", [])


def test_token_for_token_is_unparsed_not_guessed():
    tx = copy.deepcopy(SWAP)
    extra = copy.deepcopy(tx["meta"]["postTokenBalances"][0])
    extra.update(mint=MINT2 if MINT2 != WSOL else USDC, owner=SWAP_WALLET, accountIndex=99)
    extra["uiTokenAmount"] = {"amount": "5", "decimals": 6}
    tx["meta"]["postTokenBalances"].append(extra)
    tx["meta"]["preTokenBalances"] = [
        b for b in tx["meta"]["preTokenBalances"] if b["mint"] != WSOL
    ]
    tx["meta"]["postTokenBalances"] = [
        b for b in tx["meta"]["postTokenBalances"] if b["mint"] != WSOL
    ]
    assert parse_wallet_flows(tx, SWAP_WALLET)[0] in ("unparsed", "transfer")


def test_parsed_transaction_validation():
    with pytest.raises(ValueError):
        ParsedTransaction(
            source="s",
            observed_at=T0,
            received_at=T0,
            wallet_address=WALLET,
            signature=sig(1),
            slot=1,
            kind="transfer",
            flows=tuple(parse_wallet_flows(SWAP, SWAP_WALLET)[1]),
        )


# ---- flow provider / job persistence ------------------------------------------------------
def flow_provider(handler, clock, sleeper):
    clock.now = from_unix(SWAP["blockTime"]) + timedelta(seconds=60)  # tx must not be 'future'
    return RpcWalletFlowProvider(make_rpc(rpc_transport(handler), clock, sleeper))


def test_flow_provider_parses_real_tx_and_handles_gaps(clock, sleeper):
    res = run(flow_provider(lambda m, p: SWAP, clock, sleeper).get_flows(SWAP_WALLET, SWAP_SIG))
    p = res.items[0]
    assert p.kind == "swap" and p.block_time is not None and p.flows[0].side is FlowSide.BUY
    assert p.observed_at == p.block_time
    assert run(
        flow_provider(lambda m, p: None, clock, sleeper).get_flows(WALLET, sig(1))
    ).rejections
    bad = flow_provider(lambda m, p: [1], clock, sleeper)
    assert run(bad.get_flows(WALLET, sig(1))).rejections
    unsupported = flow_provider(
        lambda m, p: {"__rpc_error__": {"code": -32015, "message": "Transaction version (2)"}},
        clock,
        sleeper,
    )
    res = run(unsupported.get_flows(WALLET, sig(1)))
    assert res.items == [] and "unsupported" in res.rejections[0]


def test_flow_persistence_is_idempotent_and_tracks_pending(db, clock, sleeper):
    wallet = Wallet(source="s", observed_at=T0, received_at=T0, address=SWAP_WALLET)
    from autotrade.domain.models import WalletActivity

    act = WalletActivity(
        source="s",
        observed_at=T0,
        received_at=T0,
        wallet_address=SWAP_WALLET,
        signature=SWAP_SIG,
        slot=1,
        succeeded=True,
    )
    p = run(
        flow_provider(lambda m, q: SWAP, clock, sleeper).get_flows(SWAP_WALLET, SWAP_SIG)
    ).items[0]
    with db.session() as conn, db.transaction(conn):
        repo.upsert_wallet(conn, wallet)
        repo.insert_wallet_activity(conn, act)
    with db.session() as conn:
        assert repo.unparsed_signatures(conn, 10) == [(SWAP_WALLET, SWAP_SIG)]
    with db.session() as conn, db.transaction(conn):
        assert repo.insert_parsed_tx(conn, p, clock.now) is repo.Outcome.INSERTED
        assert repo.insert_parsed_tx(conn, p, clock.now) is repo.Outcome.DUPLICATE
    with db.session() as conn:
        assert repo.unparsed_signatures(conn, 10) == []
        assert repo.table_counts(conn)["wallet_flows"] == 1
        row = conn.execute("SELECT * FROM wallet_flows").fetchone()
        assert (row["side"], row["token_delta_raw"], row["counter_delta_raw"]) == (
            "buy",
            "3074737349",
            "-100442",
        )


# ---- features -------------------------------------------------------------------------------
def snap_row(db, at, price, *, liq="50000", b5=30, s5=10, b1=50, s1=40, received=None):
    with db.session() as conn, db.transaction(conn):
        repo.upsert_pool(
            conn,
            Pool(
                address=WALLET,
                dex="d",
                base_mint=MINT,
                quote_mint=MINT2,
                source="dexscreener",
                observed_at=T0,
                received_at=T0,
            ),
        )
        repo.insert_snapshot(
            conn,
            MarketSnapshot(
                pool_address=WALLET,
                base_mint=MINT,
                quote_mint=MINT2,
                price_quote="1",
                price_usd=price,
                liquidity_usd=liq,
                buys_5m=b5,
                sells_5m=s5,
                buys_1h=b1,
                sells_1h=s1,
                buys_24h=600,
                sells_24h=500,
                source="dexscreener",
                observed_at=at,
                received_at=received or at,
            ),
        )


def history(db, now, prices=("60", "65", "70"), **kw):
    for minutes, price in zip((15, 5, 0), prices, strict=True):
        snap_row(db, now - timedelta(minutes=minutes), price, **kw)


def feats(db, now):
    with db.session() as conn:
        return compute_features(conn, MINT, now)


def test_feature_values_are_exact(db, clock):
    now = clock.now
    history(db, now)
    f = feats(db, now)
    v = f.values
    assert Decimal(v["ret_15m"]) == Decimal("70") / Decimal("60") - 1
    assert Decimal(v["ret_5m"]) == Decimal("70") / Decimal("65") - 1
    assert v["ret_1h"] is None and "ret_1h: insufficient history" in f.notes
    assert (
        Decimal(v["imbalance_5m"]) == Decimal("0.5")
        and Decimal(v["txn_rate_accel"]) == Decimal(480) / 90
    )
    assert Decimal(v["liq_drawdown_1h"]) == 0 and v["data_points_1h"] == "3"
    assert Decimal(v["range_1h"]) == Decimal(10) / Decimal(70) and v["wallet_buyers_1h"] == "0"
    assert len(f.snapshot_ids) == 3 and f.version == "f1"


def test_no_look_ahead_future_rows_never_change_past_features(db, clock):
    now = clock.now
    history(db, now)
    before = feats(db, now)
    for minutes in (1, 5, 30):  # data observed AFTER as_of, wild values
        snap_row(db, now + timedelta(minutes=minutes), "9999", liq="1", b5=0, s5=999)
    after = feats(db, now)
    assert after.values == before.values and after.snapshot_ids == before.snapshot_ids
    later = feats(db, now + timedelta(minutes=30))
    assert later.values != before.values


def test_late_recorded_wallet_flows_are_excluded_from_past_as_of(db, clock):
    now = clock.now
    history(db, now)
    wallet = Wallet(source="s", observed_at=T0, received_at=T0, address=WALLET2)
    p = ParsedTransaction(
        source="f",
        observed_at=now - timedelta(minutes=2),
        received_at=now + timedelta(minutes=10),
        wallet_address=WALLET2,
        signature=sig(5),
        slot=1,
        kind="swap",
        flows=tuple(parse_wallet_flows(SWAP, SWAP_WALLET)[1]),
    )
    p = p.model_copy(update={"flows": (p.flows[0].model_copy(update={"mint": MINT}),)})
    with db.session() as conn, db.transaction(conn):
        repo.upsert_wallet(conn, wallet)
        repo.insert_parsed_tx(conn, p, now)
    assert feats(db, now).values["wallet_buyers_1h"] == "0"  # recorded after as_of
    assert feats(db, now + timedelta(minutes=11)).values["wallet_buyers_1h"] == "1"


def test_unknowns_are_none_not_zero(db, clock):
    f = feats(db, clock.now)
    assert f.values["ret_15m"] is None and f.values["data_points_1h"] == "0"
    snap_row(db, clock.now, "70", b5=None, s5=None, b1=None, s1=None)
    f = feats(db, clock.now)
    assert f.values["imbalance_5m"] is None and f.values["txn_rate_accel"] is None
    assert "transaction counts not reported" in f.notes


def test_history_gap_is_not_interpolated(db, clock):
    now = clock.now
    snap_row(db, now - timedelta(minutes=40), "60")  # too old for the 15m reference
    snap_row(db, now, "70")
    assert feats(db, now).values["ret_15m"] is None


# ---- signals --------------------------------------------------------------------------------
def onchain_and_safe(db, clock, verdict=True):
    """Seed the on-chain/intel data so M2 screening passes, then store the evaluation."""
    from autotrade.config import SafetyPolicy
    from autotrade.domain.models import CheckStatus, CrossCheck, TokenIntel

    now = clock.now
    with db.session() as conn, db.transaction(conn):
        repo.upsert_token(
            conn,
            Token(
                source="solana_rpc.token",
                observed_at=now,
                received_at=now,
                mint=MINT,
                decimals=9,
                authorities_known=True,
                token_program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            ),
        )
        repo.insert_intel(
            conn,
            TokenIntel(
                source="jupiter",
                observed_at=now,
                received_at=now,
                mint=MINT,
                top_holders_pct="10",
                usd_price="70",
            ),
        )
        repo.insert_cross_check(
            conn,
            CrossCheck(check_type="price_usd", mint=MINT, status=CheckStatus.OK, checked_at=now),
        )
        # make the pool old enough for the confirmation delay
        conn.execute("UPDATE pools SET created_at='2020-01-01T00:00:00.000000Z'")
        ev = evaluate_and_store(conn, [MINT], now, SafetyPolicy(), max_data_age_seconds=30)[0]
    assert (ev.verdict is Verdict.PASS) == verdict, ev.reasons


def sig_for(db, clock, policy=None):
    with db.session() as conn, db.transaction(conn):
        return generate_signal(conn, MINT, clock.now, policy or SignalPolicy())


def test_signal_emitted_with_reasons_expiry_and_traceability(db, clock):
    history(db, clock.now)
    onchain_and_safe(db, clock)
    d = sig_for(db, clock)
    s = d.signal
    assert isinstance(s, Signal) and s.strategy_version == STRATEGY_VERSION
    assert set(d.reasons) == {
        "MOMENTUM_15M",
        "MOMENTUM_5M_UP",
        "BUY_PRESSURE_5M",
        "ACTIVITY_ACCELERATING",
        "LIQUIDITY_STABLE",
    }
    assert s.score == 5 and s.expires_at == clock.now + timedelta(seconds=300)
    assert Decimal(s.invalidation["price_usd_below"]) == Decimal("70") * Decimal("0.9")
    assert Decimal(s.invalidation["liquidity_usd_below"]) == Decimal("50000") * Decimal("0.8")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM signals").fetchone()
        assert row["safety_evaluation_id"] == repo.latest_evaluation(conn, MINT)["id"]
        assert conn.execute("SELECT COUNT(*) FROM feature_sets").fetchone()[0] == 1


def test_signal_generation_is_idempotent(db, clock):
    history(db, clock.now)
    onchain_and_safe(db, clock)
    sig_for(db, clock)
    sig_for(db, clock)
    with db.session() as conn:
        assert repo.table_counts(conn)["signals"] == 1
        assert repo.table_counts(conn)["feature_sets"] == 1


def test_signals_expire(db, clock):
    history(db, clock.now)
    onchain_and_safe(db, clock)
    sig_for(db, clock)
    with db.session() as conn:
        assert len(repo.active_signals(conn, clock.now + timedelta(seconds=299))) == 1
        assert repo.active_signals(conn, clock.now + timedelta(seconds=300)) == []


def test_wallet_buyers_add_reason_but_are_never_required(db, clock):
    now = clock.now
    history(db, now)
    onchain_and_safe(db, clock)
    p = ParsedTransaction(
        source="f",
        observed_at=now - timedelta(minutes=3),
        received_at=now - timedelta(minutes=2),
        wallet_address=WALLET2,
        signature=sig(5),
        slot=1,
        kind="swap",
        flows=(parse_wallet_flows(SWAP, SWAP_WALLET)[1][0].model_copy(update={"mint": MINT}),),
    )
    with db.session() as conn, db.transaction(conn):
        repo.upsert_wallet(
            conn, Wallet(source="s", observed_at=T0, received_at=T0, address=WALLET2)
        )
        repo.insert_parsed_tx(conn, p, now)
    d = sig_for(db, clock)
    assert d.signal and "WALLET_BUYERS" in d.reasons and d.signal.score == 6


def test_no_signal_unless_safety_passed(db, clock):
    history(db, clock.now)
    assert sig_for(db, clock).reasons == ("NO_SAFETY_EVALUATION",)
    onchain_and_safe(db, clock)
    with db.session() as conn:
        conn.execute("UPDATE safety_evaluations SET verdict='reject'")
    d = sig_for(db, clock)
    assert d.signal is None and d.reasons == ("SAFETY_NOT_PASS",)
    with db.session() as conn:
        conn.execute("UPDATE safety_evaluations SET verdict='pass'")
    clock.advance(301)
    assert sig_for(db, clock).reasons == ("SAFETY_STALE",)


@pytest.mark.parametrize(
    "prices,kw,expected",
    [
        (("69", "69.5", "70"), {}, "FAILED:MOMENTUM_15M"),
        (("60", "72", "70"), {}, "FAILED:MOMENTUM_5M_UP"),
        (("60", "65", "70"), {"b5": 11, "s5": 10}, "FAILED:BUY_PRESSURE_5M"),
        (
            ("60", "65", "70"),
            {"b5": 6, "s5": 0, "b1": 1000, "s1": 1000},
            "FAILED:ACTIVITY_ACCELERATING",
        ),
    ],
)
def test_each_momentum_condition_blocks_signal(db, clock, prices, kw, expected):
    history(db, clock.now, prices, **kw)
    onchain_and_safe(db, clock)
    d = sig_for(db, clock)
    assert d.signal is None and expected in d.reasons


def test_liquidity_drop_blocks_signal(db, clock):
    now = clock.now
    snap_row(db, now - timedelta(minutes=15), "60", liq="100000")
    snap_row(db, now - timedelta(minutes=5), "65", liq="100000")
    snap_row(db, now, "70", liq="50000")
    onchain_and_safe(db, clock)
    assert "FAILED:LIQUIDITY_STABLE" in sig_for(db, clock).reasons


def test_insufficient_history_is_unknown_not_signal(db, clock):
    snap_row(db, clock.now, "70")
    onchain_and_safe(db, clock)
    d = sig_for(db, clock)
    assert d.signal is None and "UNKNOWN:ret_15m" in d.reasons


def test_signal_model_and_schema_reject_bad_expiry(db, clock):
    import sqlite3

    with pytest.raises(ValueError):
        Signal(
            mint=MINT,
            strategy_version="x",
            created_at=T0,
            expires_at=T0,
            score=1,
            reasons=(),
            invalidation={},
            feature_as_of=T0,
            safety_evaluation_id=1,
        )
    with db.session() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals(mint,strategy_version,created_at,expires_at,score,"
            "reasons,invalidation,feature_as_of,safety_evaluation_id) "
            "VALUES ('m','s','2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',"
            "1,'[]','{}','t',1)"
        )


def test_signal_policy_env_and_bounds():
    c = AppConfig.from_env(
        {"AUTOTRADE_SIGNAL_TTL_SECONDS": "120", "AUTOTRADE_SIGNAL_MIN_RET_15M": "0.05"}
    )
    assert c.signals.ttl_seconds == 120 and c.signals.min_ret_15m == Decimal("0.05")
    with pytest.raises(ValueError):
        AppConfig.from_env({"AUTOTRADE_SIGNAL_TTL_SECONDS": "1"})


def test_strategy_and_features_cannot_reach_execution_code():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "autotrade"
    imported: set[str] = set()
    for d in ("strategies", "features"):
        for path in (root / d).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imported |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
    forbidden = ("broker", "execution", "paper", "signer", "wallet_keys", "httpx")
    assert not [m for m in imported if any(f in m for f in forbidden)], imported


def test_rounding_crumb_of_quote_asset_is_not_a_payment():
    tx = copy.deepcopy(SWAP)
    for b in tx["meta"]["postTokenBalances"]:
        if b["mint"] == WSOL and b["owner"] == SWAP_WALLET:
            pre = next(
                p
                for p in tx["meta"]["preTokenBalances"]
                if p["mint"] == WSOL and p["owner"] == SWAP_WALLET
            )
            b["uiTokenAmount"]["amount"] = str(int(pre["uiTokenAmount"]["amount"]) - 1)
    assert parse_wallet_flows(tx, SWAP_WALLET) == ("transfer", [])
