"""Cross-provider checks. Provider-reported values are compared against each other and against
on-chain (RPC) facts. A missing or stale input yields UNAVAILABLE (unknown), never OK."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from autotrade.data.storage import repositories as repo
from autotrade.domain.models import CheckStatus, CrossCheck
from autotrade.domain.timeutil import parse_ts

DEX = "dexscreener"
JUP = "jupiter"
CHAIN = "solana_rpc.token"


def _fresh(ts: str, now: datetime, max_age: float) -> bool:
    return 0 <= (now - parse_ts(ts)).total_seconds() <= max_age


def _unavailable(kind: str, mint: str, now: datetime, why: str) -> CrossCheck:
    return CrossCheck(
        check_type=kind, mint=mint, status=CheckStatus.UNAVAILABLE, checked_at=now, detail=why
    )


def run_cross_checks(
    conn: sqlite3.Connection,
    mints: Sequence[str],
    now: datetime,
    *,
    max_age_seconds: float,
    price_tolerance: Decimal,
) -> list[CrossCheck]:
    out: list[CrossCheck] = []
    for mint in mints:
        snap = conn.execute(
            "SELECT price_usd, observed_at FROM market_snapshots WHERE base_mint=? AND source=? "
            "AND price_usd IS NOT NULL ORDER BY observed_at DESC, CAST(liquidity_usd AS REAL) "
            "DESC LIMIT 1",
            (mint, DEX),
        ).fetchone()
        intel = conn.execute(
            "SELECT * FROM token_intel WHERE mint=? AND source=? ORDER BY observed_at DESC LIMIT 1",
            (mint, JUP),
        ).fetchone()
        chain = conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()
        out.append(_price(mint, snap, intel, now, max_age_seconds, price_tolerance))
        out += _onchain(mint, intel, chain, now, max_age_seconds)
    for c in out:
        repo.insert_cross_check(conn, c)
    return out


def _price(mint, snap, intel, now, max_age, tol) -> CrossCheck:  # type: ignore[no-untyped-def]
    if snap is None or not _fresh(snap["observed_at"], now, max_age):
        return _unavailable("price_usd", mint, now, "no fresh DEX Screener price")
    if (
        intel is None
        or intel["usd_price"] is None
        or not _fresh(intel["observed_at"], now, max_age)
    ):
        return _unavailable("price_usd", mint, now, "no fresh Jupiter price")
    a, b = Decimal(snap["price_usd"]), Decimal(intel["usd_price"])
    deviation = abs(a - b) / b
    status = CheckStatus.DISCREPANCY if deviation > tol else CheckStatus.OK
    return CrossCheck(
        check_type="price_usd",
        mint=mint,
        status=status,
        checked_at=now,
        source_a=DEX,
        value_a=str(a),
        source_b=JUP,
        value_b=str(b),
        deviation=deviation,
        detail=f"tolerance {tol}",
    )


def _onchain(mint, intel, chain, now, max_age) -> list[CrossCheck]:  # type: ignore[no-untyped-def]
    kinds = ("decimals", "mint_authority", "freeze_authority")
    if chain is None or not chain["authorities_known"]:
        return [_unavailable(k, mint, now, "no on-chain (RPC) token record") for k in kinds]
    if intel is None or not _fresh(intel["observed_at"], now, max_age):
        return [_unavailable(k, mint, now, "no fresh Jupiter token record") for k in kinds]
    pairs = {
        "decimals": (intel["decimals"], chain["decimals"]),
        "mint_authority": (intel["mint_authority_disabled"], int(chain["mint_authority"] is None)),
        "freeze_authority": (
            intel["freeze_authority_disabled"],
            int(chain["freeze_authority"] is None),
        ),
    }
    out = []
    for kind, (claimed, onchain) in pairs.items():
        if claimed is None:
            out.append(_unavailable(kind, mint, now, "Jupiter did not report this field"))
            continue
        status = CheckStatus.OK if claimed == onchain else CheckStatus.DISCREPANCY
        out.append(
            CrossCheck(
                check_type=kind,
                mint=mint,
                status=status,
                checked_at=now,
                source_a=JUP,
                value_a=str(claimed),
                source_b=CHAIN,
                value_b=str(onchain),
                detail="provider-reported vs on-chain",
            )
        )
    return out
