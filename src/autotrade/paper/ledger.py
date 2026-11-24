"""Append-only paper ledger (USD) and its independent reconciliation.

Cash is the sum of ledger entries. Reconciliation recomputes cash, open quantities and total
PnL from fills/orders/positions and compares: any mismatch is a critical error that halts new
entries (the caller engages the kill switch)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from autotrade.domain.timeutil import fmt_ts

ZERO = Decimal(0)
TOLERANCE = Decimal("1e-9")


def ensure_deposit(conn: sqlite3.Connection, now: datetime, amount_usd: Decimal) -> bool:
    """Idempotent initial funding. Returns True if it was created now."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO ledger_entries(ts,kind,amount_usd,note) VALUES (?,?,?,?)",
        (fmt_ts(now), "deposit", str(amount_usd), "initial paper equity"),
    )
    return cur.rowcount == 1


def post(
    conn: sqlite3.Connection,
    now: datetime,
    kind: str,
    amount: Decimal,
    order_id: int | None,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ledger_entries(ts,kind,amount_usd,order_id,note) VALUES (?,?,?,?,?)",
        (fmt_ts(now), kind, str(amount), order_id, note),
    )


def cash(conn: sqlite3.Connection) -> Decimal:
    return sum(
        (Decimal(r["amount_usd"]) for r in conn.execute("SELECT amount_usd FROM ledger_entries")),
        ZERO,
    )


def last_prices(conn: sqlite3.Connection, mints: list[str]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for m in mints:
        row = conn.execute(
            "SELECT price_usd FROM market_snapshots WHERE base_mint=? AND source='dexscreener' "
            "AND price_usd IS NOT NULL ORDER BY observed_at DESC, CAST(liquidity_usd AS REAL) DESC "
            "LIMIT 1",
            (m,),
        ).fetchone()
        if row is not None:
            out[m] = Decimal(row["price_usd"])
    return out


def open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM paper_positions WHERE closed_at IS NULL ORDER BY id"))


def equity(conn: sqlite3.Connection) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    """(equity, cash, mark prices). Open positions without any quote are valued at cost basis
    (never silently at zero) and flagged by a missing entry in the price map."""
    c = cash(conn)
    pos = open_positions(conn)
    prices = last_prices(conn, [p["mint"] for p in pos])
    total = c
    for p in pos:
        mark = prices.get(p["mint"])
        total += Decimal(p["qty"]) * mark if mark is not None else Decimal(p["cost_basis_usd"])
    return total, c, prices


def reconcile(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    dep = conn.execute("SELECT COUNT(*) n FROM ledger_entries WHERE kind='deposit'").fetchone()["n"]
    if dep != 1:
        problems.append(f"expected exactly 1 deposit, found {dep}")
        return problems
    deposit = Decimal(
        conn.execute("SELECT amount_usd FROM ledger_entries WHERE kind='deposit'").fetchone()[
            "amount_usd"
        ]
    )

    # 1. order/fill integrity
    for r in conn.execute(
        "SELECT o.id, o.status, COUNT(f.id) n FROM paper_orders o LEFT JOIN paper_fills f "
        "ON f.order_id=o.id GROUP BY o.id"
    ):
        expected = 1 if r["status"] == "filled" else 0
        if r["n"] != expected:
            problems.append(f"order {r['id']} status={r['status']} has {r['n']} fills")

    # 2. cash recomputed from fills and failed orders must equal the ledger
    derived = deposit
    for f in conn.execute(
        "SELECT o.kind, f.gross_usd, f.fee_usd FROM paper_fills f JOIN paper_orders o "
        "ON o.id=f.order_id"
    ):
        gross, fee = Decimal(f["gross_usd"]), Decimal(f["fee_usd"])
        derived += (-gross if f["kind"] == "entry" else gross) - fee
    failed_fees = sum(
        (
            Decimal(r["amount_usd"])
            for r in conn.execute(
                "SELECT amount_usd FROM ledger_entries WHERE kind='failed_tx_fee'"
            )
        ),
        ZERO,
    )
    derived += failed_fees
    ledger_cash = cash(conn)
    if abs(derived - ledger_cash) > TOLERANCE:
        problems.append(f"cash mismatch: ledger {ledger_cash} vs derived {derived}")

    # 3. open quantity per mint = bought - sold
    held: dict[str, Decimal] = {}
    for f in conn.execute(
        "SELECT o.kind, o.mint, f.qty FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
    ):
        held[f["mint"]] = held.get(f["mint"], ZERO) + (
            Decimal(f["qty"]) if f["kind"] == "entry" else -Decimal(f["qty"])
        )
    open_qty = {p["mint"]: Decimal(p["qty"]) for p in open_positions(conn)}
    for mint in set(held) | set(open_qty):
        if abs(held.get(mint, ZERO) - open_qty.get(mint, ZERO)) > TOLERANCE:
            problems.append(
                f"quantity mismatch for {mint}: fills {held.get(mint, ZERO)} vs "
                f"positions {open_qty.get(mint, ZERO)}"
            )

    # 4. equity - deposit == realized + unrealized (+ failed fees, which are realized costs)
    eq, _, prices = equity(conn)
    realized = sum(
        (
            Decimal(r["realized_pnl_usd"])
            for r in conn.execute(
                "SELECT realized_pnl_usd FROM paper_positions WHERE closed_at IS NOT NULL"
            )
        ),
        ZERO,
    )
    unrealized = ZERO
    for p in open_positions(conn):
        mark = prices.get(p["mint"])
        value = Decimal(p["qty"]) * mark if mark is not None else Decimal(p["cost_basis_usd"])
        unrealized += value - Decimal(p["cost_basis_usd"]) - Decimal(p["entry_cost_usd"])
    if abs((eq - deposit) - (realized + unrealized + failed_fees)) > TOLERANCE:
        problems.append(
            f"pnl mismatch: equity-deposit {eq - deposit} vs realized {realized} + "
            f"unrealized {unrealized} + failed fees {failed_fees}"
        )
    return problems
