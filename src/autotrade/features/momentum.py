"""Point-in-time feature builder (version f1).

NO LOOK-AHEAD: every query is bounded by `observed_at <= as_of`; changing or adding rows after
`as_of` can never change the result. A feature without enough history is None (unknown) with a
note; it is never interpolated, defaulted to zero, or filled from newer data.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from autotrade.domain.models import FeatureSet
from autotrade.domain.timeutil import fmt_ts, parse_ts

FEATURES_VERSION = "f1"
SNAPSHOT_SOURCE = "dexscreener"
HISTORY = timedelta(hours=1)
WINDOWS = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1)}


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def compute_features(conn: sqlite3.Connection, mint: str, as_of: datetime) -> FeatureSet:
    notes: list[str] = []
    values: dict[str, str | None] = {}
    pool = conn.execute(
        "SELECT pool_address FROM market_snapshots WHERE base_mint=? AND source=? "
        "AND observed_at <= ? ORDER BY observed_at DESC, CAST(liquidity_usd AS REAL) DESC LIMIT 1",
        (mint, SNAPSHOT_SOURCE, fmt_ts(as_of)),
    ).fetchone()
    rows: list[sqlite3.Row] = []
    if pool is not None:
        rows = list(
            conn.execute(
                "SELECT * FROM market_snapshots WHERE pool_address=? AND source=? "
                "AND observed_at <= ? AND observed_at >= ? ORDER BY observed_at DESC",
                (pool["pool_address"], SNAPSHOT_SOURCE, fmt_ts(as_of), fmt_ts(as_of - HISTORY)),
            )
        )
    out: dict[str, str | None] = {
        k: None
        for k in (
            "price_usd",
            "liquidity_usd",
            "ret_5m",
            "ret_15m",
            "ret_1h",
            "txn_rate_accel",
            "imbalance_5m",
            "imbalance_1h",
            "liq_drawdown_1h",
            "range_1h",
        )
    }
    series = [(parse_ts(r["observed_at"]), r) for r in rows]
    if not series:
        notes.append("no snapshots at/before as_of")
        values = {**out, "data_points_1h": "0", **_wallet(conn, mint, as_of)}
        return FeatureSet(
            mint=mint, as_of=as_of, version=FEATURES_VERSION, values=values, notes=tuple(notes)
        )
    t_now, now = series[0]
    price = _price(now)
    for name, window in WINDOWS.items():
        ref = _nearest(series, t_now - window)
        if ref is None:
            notes.append(f"ret_{name}: insufficient history")
            continue
        p0 = _price(ref)
        if price is not None and p0 is not None and p0 > 0:
            out[f"ret_{name}"] = _s(price / p0 - 1)
        else:
            notes.append(f"ret_{name}: price unavailable")
    out["price_usd"] = _s(price)
    out["liquidity_usd"] = now["liquidity_usd"]
    out["imbalance_5m"] = _s(_imbalance(now["buys_5m"], now["sells_5m"]))
    out["imbalance_1h"] = _s(_imbalance(now["buys_1h"], now["sells_1h"]))
    if None in (now["buys_5m"], now["sells_5m"], now["buys_1h"], now["sells_1h"]):
        notes.append("transaction counts not reported")
    else:
        n1h = now["buys_1h"] + now["sells_1h"]
        if n1h > 0:
            out["txn_rate_accel"] = _s(Decimal((now["buys_5m"] + now["sells_5m"]) * 12) / n1h)
    liqs = [Decimal(r["liquidity_usd"]) for _, r in series if r["liquidity_usd"] is not None]
    if now["liquidity_usd"] is not None and liqs and max(liqs) > 0:
        out["liq_drawdown_1h"] = _s(1 - Decimal(now["liquidity_usd"]) / max(liqs))
    prices = [p for _, r in series if (p := _price(r)) is not None]
    if price is not None and len(prices) >= 3 and price > 0:
        out["range_1h"] = _s((max(prices) - min(prices)) / price)
    values = {**out, "data_points_1h": str(len(series)), **_wallet(conn, mint, as_of)}
    return FeatureSet(
        mint=mint,
        as_of=as_of,
        version=FEATURES_VERSION,
        values=values,
        notes=tuple(notes),
        snapshot_ids=tuple(r["id"] for _, r in series),
    )


def _nearest(series: list[tuple[datetime, sqlite3.Row]], target: datetime) -> sqlite3.Row | None:
    window = (series[0][0] - target).total_seconds()
    tol = timedelta(seconds=min(300.0, max(60.0, window / 3)))
    for t, r in series:  # newest first: first row at/before target is the closest one
        if t <= target:
            return r if target - t <= tol else None
    return None


def _price(r: sqlite3.Row) -> Decimal | None:
    return Decimal(r["price_usd"]) if r["price_usd"] is not None else None


def _imbalance(buys: int | None, sells: int | None) -> Decimal | None:
    if buys is None or sells is None or buys + sells == 0:
        return None
    return Decimal(buys - sells) / Decimal(buys + sells)


def _wallet(conn: sqlite3.Connection, mint: str, as_of: datetime) -> dict[str, str | None]:
    """Distinct watched wallets with net buying / selling of `mint` in the last hour (context
    only; wallet labels are hypotheses, not facts)."""
    rows = conn.execute(
        "SELECT wallet_address, side, COUNT(*) n FROM wallet_flows WHERE mint=? "
        "AND observed_at <= ? AND received_at <= ? AND observed_at > ? "
        "GROUP BY wallet_address, side",
        (mint, fmt_ts(as_of), fmt_ts(as_of), fmt_ts(as_of - HISTORY)),
    )
    net: dict[str, int] = {}
    for r in rows:
        net[r["wallet_address"]] = net.get(r["wallet_address"], 0) + (
            r["n"] if r["side"] == "buy" else -r["n"]
        )
    return {
        "wallet_buyers_1h": str(sum(1 for v in net.values() if v > 0)),
        "wallet_sellers_1h": str(sum(1 for v in net.values() if v < 0)),
    }
