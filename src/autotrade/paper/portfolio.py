"""Ledger-backed portfolio for the risk gate (replaces the flat placeholder)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from autotrade.config import AppConfig
from autotrade.domain.timeutil import fmt_ts
from autotrade.paper import ledger
from autotrade.risk.portfolio import FlatPaperPortfolio, PortfolioState, Position


def load_portfolio(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> PortfolioState:
    """Equity/cash/positions from the paper ledger (IDR for the gate). Before the ledger is
    funded, or without an FX rate, the explicit flat portfolio is returned."""
    fx = config.risk.usd_idr_rate
    funded = conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone()
    if fx is None or not funded:
        return FlatPaperPortfolio(config.initial_equity_idr).load()
    eq, cash, prices = ledger.equity(conn)
    day = now.date().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO daily_equity(day,start_equity_usd) VALUES (?,?)", (day, str(eq))
    )
    sod = Decimal(
        conn.execute("SELECT start_equity_usd FROM daily_equity WHERE day=?", (day,)).fetchone()[0]
    )
    day_start = fmt_ts(datetime.fromisoformat(day + "T00:00:00+00:00"))
    realized = sum(
        (
            Decimal(r["realized_pnl_usd"])
            for r in conn.execute(
                "SELECT realized_pnl_usd FROM paper_positions WHERE closed_at >= ?", (day_start,)
            )
        ),
        Decimal(0),
    )
    positions = tuple(
        Position(
            p["mint"],
            Decimal(p["qty"])
            * prices.get(p["mint"], Decimal(p["cost_basis_usd"]) / Decimal(p["qty"]))
            * fx,
        )
        for p in ledger.open_positions(conn)
    )
    return PortfolioState(eq * fx, cash * fx, sod * fx, realized * fx, positions)
