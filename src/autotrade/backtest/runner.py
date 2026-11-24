"""Backtest orchestration: development runs, walk-forward folds, the untouched holdout."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from autotrade.backtest import registry
from autotrade.backtest.config import BacktestConfig
from autotrade.backtest.data import timeline
from autotrade.backtest.engine import Result, simulate
from autotrade.backtest.metrics import result_hash, summarize
from autotrade.config import AppConfig

EPS = timedelta(microseconds=1)


@dataclass
class Report:
    kind: str
    window: tuple[datetime, datetime]
    summary: dict[str, Any]
    result_hash: str
    experiment_id: int | None = None
    contaminated: bool = False
    folds: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _run(
    conn: sqlite3.Connection, config: AppConfig, bt: BacktestConfig, start: datetime, end: datetime
) -> tuple[Result, dict[str, Any], str]:
    res = simulate(conn, config, bt, start, end)
    summary = summarize(res)
    return res, summary, result_hash(summary, res)


def _bounds(conn: sqlite3.Connection) -> tuple[datetime, datetime]:
    span = timeline(conn)
    if span is None:
        raise ValueError("no stored market snapshots: nothing to replay (collect data first)")
    return span


def dev_run(
    conn: sqlite3.Connection,
    config: AppConfig,
    bt: BacktestConfig,
    now: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Report:
    lo, hi = _bounds(conn)
    lock = registry.get_lock(conn)
    notes: list[str] = []
    cap = lock if lock is not None else hi + EPS
    if lock is None:
        notes.append("NO HOLDOUT LOCKED: this is in-sample only; run `backtest lock` first")
    s = max(start or lo, lo)
    e = min(end or cap, cap)  # development can never reach the holdout
    if s >= e:
        raise ValueError("empty development window")
    _, summary, h = _run(conn, config, bt, s, e)
    eid, _ = registry.record(conn, now, "dev", registry.config_hash(config, bt), s, e, summary, h)
    return Report("dev", (s, e), summary, h, eid, notes=notes)


def walk_forward(
    conn: sqlite3.Connection, config: AppConfig, bt: BacktestConfig, now: datetime, folds: int
) -> Report:
    if folds < 2:
        raise ValueError("walk-forward needs at least 2 folds")
    lo, hi = _bounds(conn)
    lock = registry.get_lock(conn)
    if lock is None:
        raise ValueError("lock the holdout first (`autotrade backtest lock`)")
    end = min(lock, hi + EPS)
    if end <= lo:
        raise ValueError("no development data before the holdout")
    step = (end - lo) / folds
    out: list[dict[str, Any]] = []
    all_returns: list[Decimal] = []
    hashes: list[str] = []
    for i in range(folds):
        s = lo + step * i
        e = end if i == folds - 1 else lo + step * (i + 1)
        res, summary, h = _run(conn, config, bt, s, e)
        hashes.append(h)
        all_returns += [t.net_return for t in res.trades]
        out.append(
            {
                "fold": i + 1,
                "window": summary["window"],
                "trades": summary["trades"],
                "net_pnl_usd": summary["net_pnl_usd"],
                "expectancy_return_per_trade": summary["expectancy_return_per_trade"],
            }
        )
    positive = sum(1 for f in out if f["net_pnl_usd"] is not None and Decimal(f["net_pnl_usd"]) > 0)
    pooled = sum(all_returns, Decimal(0)) / len(all_returns) if all_returns else None
    summary = {
        "folds": folds,
        "trades": len(all_returns),
        "positive_folds": positive,
        "pooled_expectancy_return_per_trade": None if pooled is None else str(pooled),
        "sample_warning": None
        if len(all_returns) >= 30
        else f"only {len(all_returns)} trades pooled (< 30): not statistical evidence",
    }
    h = hashlib.sha256("|".join(hashes).encode()).hexdigest()
    eid, _ = registry.record(
        conn, now, "walkforward", registry.config_hash(config, bt), lo, end, summary, h
    )
    return Report("walkforward", (lo, end), summary, h, eid, folds=out)


def holdout_run(
    conn: sqlite3.Connection, config: AppConfig, bt: BacktestConfig, now: datetime
) -> Report:
    lo, hi = _bounds(conn)
    lock = registry.get_lock(conn)
    if lock is None:
        raise ValueError("lock the holdout first (`autotrade backtest lock`)")
    end = hi + EPS
    if lock >= end:
        raise ValueError("holdout window is empty (no data after the lock)")
    _, summary, h = _run(conn, config, bt, lock, end)
    eid, tainted = registry.record(
        conn, now, "holdout", registry.config_hash(config, bt), lock, end, summary, h
    )
    notes = []
    if tainted:
        notes.append(
            "CONTAMINATED: the holdout was already evaluated with a different "
            "configuration; these numbers are no longer out-of-sample evidence"
        )
    return Report("holdout", (lock, end), summary, h, eid, tainted, notes=notes)
