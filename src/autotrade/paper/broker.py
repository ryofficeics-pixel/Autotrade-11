"""Paper broker: turns approved risk decisions into SIMULATED orders and fills.

Nothing is signed, built or sent. Fills are arithmetic over stored DEX Screener snapshots using
the conservative model in execution_model.py (executable price, never mid). Every state change
is in SQLite, so a restart resumes exactly where it stopped, and a risk decision can create at
most one order (UNIQUE decision_id)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from autotrade.config import AppConfig
from autotrade.domain.timeutil import fmt_ts, parse_ts
from autotrade.paper import ledger
from autotrade.paper.execution_model import FillParams, buy_fill, sell_fill, tx_fails

ZERO = Decimal(0)


@dataclass(frozen=True)
class Event:
    order_id: int
    kind: str  # entry | exit
    mint: str
    outcome: str  # filled | failed | missed | pending | created
    detail: str = ""


def _fp(config: AppConfig) -> FillParams:
    rk, pp = config.risk, config.paper
    return FillParams(
        rk.dex_fee_fraction, rk.network_fee_usd, pp.slippage_buffer, rk.max_price_impact_fraction
    )


def submit_intents(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Event]:
    """Create one pending entry order per approved risk decision (idempotent by decision id)."""
    pp = config.paper
    events: list[Event] = []
    rows = conn.execute(
        "SELECT d.*, s.invalidation FROM risk_decisions d JOIN signals s ON s.id=d.signal_id "
        "WHERE d.approved=1 AND NOT EXISTS (SELECT 1 FROM paper_orders o WHERE o.decision_id=d.id) "
        "ORDER BY d.id"
    ).fetchall()
    for d in rows:
        floor = json.loads(d["invalidation"]).get("liquidity_usd_below")
        decided = parse_ts(d["decided_at"])
        cur = conn.execute(
            "INSERT INTO paper_orders(kind,decision_id,mint,created_at,exec_after,expires_at,"
            "notional_usd,stop_usd,liq_floor_usd,reason,status,updated_at) "
            "VALUES ('entry',?,?,?,?,?,?,?,?,'entry','pending',?)",
            (
                d["id"],
                d["mint"],
                fmt_ts(now),
                fmt_ts(decided + timedelta(seconds=pp.latency_seconds)),
                fmt_ts(decided + timedelta(seconds=pp.max_pending_seconds)),
                d["notional_usd"],
                d["stop_price_usd"],
                floor,
                fmt_ts(now),
            ),
        )
        events.append(
            Event(
                int(cur.lastrowid or 0),
                "entry",
                d["mint"],
                "created",
                f"notional_usd={d['notional_usd']}",
            )
        )
    return events


def _latest(conn: sqlite3.Connection, mint: str, now: datetime) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM market_snapshots WHERE base_mint=? AND source='dexscreener' "
        "AND price_usd IS NOT NULL AND liquidity_usd IS NOT NULL AND observed_at <= ? "
        "ORDER BY observed_at DESC, CAST(liquidity_usd AS REAL) DESC LIMIT 1",
        (mint, fmt_ts(now)),
    ).fetchone()
    return row


def _first_at_or_after(
    conn: sqlite3.Connection, mint: str, t: str, now: datetime
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM market_snapshots WHERE base_mint=? AND source='dexscreener' "
        "AND price_usd IS NOT NULL AND liquidity_usd IS NOT NULL AND observed_at >= ? "
        "AND observed_at <= ? ORDER BY observed_at ASC, CAST(liquidity_usd AS REAL) DESC LIMIT 1",
        (mint, t, fmt_ts(now)),
    ).fetchone()
    return row


def detect_exits(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Event]:
    """Create exit orders for open positions that hit stop / liquidity floor / time / death."""
    pp = config.paper
    events: list[Event] = []
    for pos in ledger.open_positions(conn):
        pending = conn.execute(
            "SELECT 1 FROM paper_orders WHERE position_id=? AND status='pending'", (pos["id"],)
        ).fetchone()
        if pending:
            continue
        quote = _latest(conn, pos["mint"], now)
        reason = ""
        if (
            quote is None
            or (now - parse_ts(quote["observed_at"])).total_seconds() > pp.dead_after_seconds
        ):
            reason = "dead"
        else:
            price, liq = Decimal(quote["price_usd"]), Decimal(quote["liquidity_usd"])
            if price <= Decimal(pos["stop_usd"]):
                reason = "stop"
            elif pos["liq_floor_usd"] is not None and liq <= Decimal(pos["liq_floor_usd"]):
                reason = "liquidity"
            elif (now - parse_ts(pos["opened_at"])).total_seconds() >= pp.max_hold_seconds:
                reason = "time"
        if not reason:
            continue
        cur = conn.execute(
            "INSERT INTO paper_orders(kind,position_id,mint,created_at,exec_after,expires_at,qty,"
            "reason,status,updated_at) VALUES ('exit',?,?,?,?,?,?,?,'pending',?)",
            (
                pos["id"],
                pos["mint"],
                fmt_ts(now),
                fmt_ts(now + timedelta(seconds=pp.latency_seconds)),
                fmt_ts(now + timedelta(days=3650)),
                pos["qty"],
                reason,
                fmt_ts(now),
            ),
        )
        events.append(Event(int(cur.lastrowid or 0), "exit", pos["mint"], "created", reason))
    return events


def process_orders(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Event]:
    events: list[Event] = []
    for order in conn.execute(
        "SELECT * FROM paper_orders WHERE status='pending' ORDER BY id"
    ).fetchall():
        events.append(_process_one(conn, order, now, config))
    return [e for e in events if e.outcome != "pending"]


def _finish(
    conn: sqlite3.Connection, order: sqlite3.Row, now: datetime, status: str, why: str
) -> Event:
    conn.execute(
        "UPDATE paper_orders SET status=?, status_reason=?, updated_at=? WHERE id=?",
        (status, why, fmt_ts(now), order["id"]),
    )
    return Event(order["id"], order["kind"], order["mint"], status, why)


def _process_one(
    conn: sqlite3.Connection, order: sqlite3.Row, now: datetime, config: AppConfig
) -> Event:
    pp, fp = config.paper, _fp(config)
    mint = order["mint"]
    dead_exit = order["kind"] == "exit" and order["reason"] == "dead"
    tick = _first_at_or_after(conn, mint, order["exec_after"], now)
    if dead_exit:
        if now < parse_ts(order["exec_after"]):
            return Event(order["id"], order["kind"], mint, "pending")  # latency applies too
        tick = _latest(conn, mint, now)  # last known quote; no fresh quote exists by definition
    if tick is None:
        if now > parse_ts(order["expires_at"]):
            return _finish(conn, order, now, "missed", "no executable quote before expiry")
        return Event(order["id"], order["kind"], mint, "pending")
    if (
        tx_fails(
            pp.draw_label, mint, parse_ts(tick["observed_at"]), order["kind"], pp.failed_tx_rate
        )
        and not dead_exit
    ):
        ledger.post(conn, now, "failed_tx_fee", -fp.network_fee_usd, order["id"], "failed tx")
        return _finish(conn, order, now, "failed", "simulated transaction failure (fee paid)")
    mid, liq = Decimal(tick["price_usd"]), Decimal(tick["liquidity_usd"])
    if order["kind"] == "entry":
        return _fill_entry(conn, order, tick, mid, liq, now, fp)
    return _fill_exit(conn, order, tick, mid, liq, now, fp, config, dead_exit)


def _fill_entry(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    tick: sqlite3.Row,
    mid: Decimal,
    liq: Decimal,
    now: datetime,
    fp: FillParams,
) -> Event:
    notional = Decimal(order["notional_usd"])
    stop = Decimal(order["stop_usd"])
    buy = buy_fill(mid, liq, notional, fp)
    if buy is None:
        return _finish(conn, order, now, "missed", "price impact above the cap at execution")
    if mid <= stop:
        return _finish(conn, order, now, "missed", "price already at/below the stop")
    if notional + buy.entry_cost > ledger.cash(conn):
        return _finish(conn, order, now, "missed", "insufficient paper cash")
    conn.execute(
        "INSERT INTO paper_fills(order_id,filled_at,mid_price,fill_price,qty,gross_usd,fee_usd,"
        "impact,snapshot_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            order["id"],
            fmt_ts(now),
            str(mid),
            str(buy.fill_price),
            str(buy.qty),
            str(notional),
            str(buy.entry_cost),
            str(buy.impact),
            tick["id"],
        ),
    )
    conn.execute(
        "INSERT INTO paper_positions(mint,qty,cost_basis_usd,entry_fill_price,entry_cost_usd,"
        "stop_usd,liq_floor_usd,opened_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            order["mint"],
            str(buy.qty),
            str(notional),
            str(buy.fill_price),
            str(buy.entry_cost),
            order["stop_usd"],
            order["liq_floor_usd"],
            fmt_ts(now),
        ),
    )
    ledger.post(conn, now, "buy", -notional, order["id"])
    ledger.post(conn, now, "fee", -buy.entry_cost, order["id"], "dex + network fee")
    return _finish(conn, order, now, "filled", f"qty={buy.qty} fill={buy.fill_price}")


def _fill_exit(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    tick: sqlite3.Row,
    mid: Decimal,
    liq: Decimal,
    now: datetime,
    fp: FillParams,
    config: AppConfig,
    dead: bool,
) -> Event:
    pos = conn.execute(
        "SELECT * FROM paper_positions WHERE id=?", (order["position_id"],)
    ).fetchone()
    qty = Decimal(pos["qty"])
    sell = sell_fill(mid, liq, qty, fp, haircut=config.paper.dead_token_haircut if dead else None)
    conn.execute(
        "INSERT INTO paper_fills(order_id,filled_at,mid_price,fill_price,qty,gross_usd,fee_usd,"
        "impact,snapshot_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            order["id"],
            fmt_ts(now),
            str(mid),
            str(sell.fill_price),
            str(qty),
            str(sell.proceeds),
            str(sell.fee),
            str(sell.impact),
            tick["id"],
        ),
    )
    realized = (
        sell.proceeds - sell.fee - Decimal(pos["cost_basis_usd"]) - Decimal(pos["entry_cost_usd"])
    )
    conn.execute(
        "UPDATE paper_positions SET closed_at=?, exit_reason=?, realized_pnl_usd=? WHERE id=?",
        (fmt_ts(now), order["reason"], str(realized), pos["id"]),
    )
    ledger.post(conn, now, "sell", sell.proceeds, order["id"])
    ledger.post(conn, now, "fee", -sell.fee, order["id"], "dex + network fee")
    return _finish(conn, order, now, "filled", f"{order['reason']} realized={realized}")


def run_cycle(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Event]:
    """One broker pass: fund, accept intents, trigger exits, fill what is executable."""
    fx = config.risk.usd_idr_rate
    if fx is None:
        return []  # equity cannot be expressed in USD without an explicit FX rate
    ledger.ensure_deposit(conn, now, config.initial_equity_idr / fx)
    events = submit_intents(conn, now, config)
    events += detect_exits(conn, now, config)
    events += process_orders(conn, now, config)
    return events
