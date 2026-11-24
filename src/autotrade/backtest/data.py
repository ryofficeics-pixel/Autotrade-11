"""Replay inputs read from the local store. Nothing is fabricated: no stored data, no replay."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from autotrade.domain.timeutil import fmt_ts, parse_ts

SOURCE = "dexscreener"


@dataclass(frozen=True)
class Tick:
    t: datetime
    mint: str
    price: Decimal
    liquidity: Decimal


def timeline(conn: sqlite3.Connection) -> tuple[datetime, datetime] | None:
    row = conn.execute(
        "SELECT MIN(observed_at) lo, MAX(observed_at) hi FROM market_snapshots WHERE source=?",
        (SOURCE,),
    ).fetchone()
    if row["lo"] is None:
        return None
    return parse_ts(row["lo"]), parse_ts(row["hi"])


def load_ticks(
    conn: sqlite3.Connection, start: datetime, end: datetime, mints: list[str] | None = None
) -> list[Tick]:
    """Best-liquidity pool per (mint, time), start <= t < end, chronological and deterministic."""
    rows = conn.execute(
        "SELECT base_mint, observed_at, price_usd, liquidity_usd FROM market_snapshots "
        "WHERE source=? AND price_usd IS NOT NULL AND liquidity_usd IS NOT NULL "
        "AND observed_at >= ? AND observed_at < ? "
        "ORDER BY observed_at ASC, base_mint ASC, CAST(liquidity_usd AS REAL) DESC, id ASC",
        (SOURCE, fmt_ts(start), fmt_ts(end)),
    )
    seen: set[tuple[str, str]] = set()
    ticks: list[Tick] = []
    for r in rows:
        key = (r["base_mint"], r["observed_at"])
        if key in seen or (mints is not None and r["base_mint"] not in mints):
            continue
        seen.add(key)
        price, liq = Decimal(r["price_usd"]), Decimal(r["liquidity_usd"])
        if price > 0 and liq > 0:
            ticks.append(Tick(parse_ts(r["observed_at"]), r["base_mint"], price, liq))
    return ticks


def safety_passed(conn: sqlite3.Connection, mint: str, t: datetime, max_age: float) -> bool:
    """Point-in-time: the latest stored evaluation at/before t, if recent enough, must be PASS."""
    row = conn.execute(
        "SELECT verdict, evaluated_at FROM safety_evaluations WHERE mint=? AND evaluated_at <= ? "
        "ORDER BY evaluated_at DESC, id DESC LIMIT 1",
        (mint, fmt_ts(t)),
    ).fetchone()
    if row is None or row["verdict"] != "pass":
        return False
    return (t - parse_ts(row["evaluated_at"])).total_seconds() <= max_age


def price_check_ok(conn: sqlite3.Connection, mint: str, t: datetime, max_age: float) -> bool:
    row = conn.execute(
        "SELECT status, checked_at FROM cross_checks WHERE mint=? AND check_type='price_usd' "
        "AND checked_at <= ? ORDER BY checked_at DESC, id DESC LIMIT 1",
        (mint, fmt_ts(t)),
    ).fetchone()
    if row is None or row["status"] != "ok":
        return False
    return (t - parse_ts(row["checked_at"])).total_seconds() <= max_age
