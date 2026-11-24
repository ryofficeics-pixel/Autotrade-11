"""Experiment registry and the untouched-holdout guard.

The holdout start is fixed ONCE (`lock`); development runs can never reach past it. A holdout
evaluation run with a different configuration than an earlier holdout evaluation is recorded as
CONTAMINATED: the holdout has been used to choose between configurations and is no longer
out-of-sample evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from autotrade.backtest.config import BacktestConfig
from autotrade.backtest.data import timeline
from autotrade.config import AppConfig
from autotrade.domain.timeutil import fmt_ts, parse_ts
from autotrade.features.momentum import FEATURES_VERSION
from autotrade.safety.screening import RULESET_VERSION
from autotrade.strategies.hybrid import STRATEGY_VERSION


def config_hash(config: AppConfig, bt: BacktestConfig) -> str:
    payload = {
        "risk": json.loads(config.risk.model_dump_json()),
        "signals": json.loads(config.signals.model_dump_json()),
        "initial_equity_idr": str(config.initial_equity_idr),
        "bt": json.loads(bt.model_dump_json()),
        "versions": [STRATEGY_VERSION, FEATURES_VERSION, RULESET_VERSION],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def get_lock(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT holdout_start FROM backtest_lock WHERE id=1").fetchone()
    return None if row is None else parse_ts(row["holdout_start"])


def lock_holdout(
    conn: sqlite3.Connection, now: datetime, fraction: Decimal
) -> tuple[datetime, bool]:
    """Fix the holdout boundary once. Returns (holdout_start, created_now)."""
    existing = get_lock(conn)
    if existing is not None:
        return existing, False
    span = timeline(conn)
    if span is None or span[0] == span[1]:
        raise ValueError("not enough stored market history to lock a holdout")
    lo, hi = span
    start = lo + (hi - lo) * float(1 - fraction)
    conn.execute(
        "INSERT INTO backtest_lock(id,holdout_start,fraction,locked_at) VALUES (1,?,?,?)",
        (fmt_ts(start), str(fraction), fmt_ts(now)),
    )
    return start, True


def record(
    conn: sqlite3.Connection,
    now: datetime,
    kind: str,
    cfg_hash: str,
    start: datetime,
    end: datetime,
    summary: dict[str, Any],
    res_hash: str,
) -> tuple[int, bool]:
    contaminated = False
    if kind == "holdout":
        other = conn.execute(
            "SELECT COUNT(*) n FROM experiments WHERE kind='holdout' "
            "AND (config_hash <> ? OR contaminated = 1)",
            (cfg_hash,),
        ).fetchone()["n"]
        contaminated = other > 0
        if contaminated:  # the holdout was used to compare configurations: all its runs are tainted
            conn.execute("UPDATE experiments SET contaminated=1 WHERE kind='holdout'")
    cur = conn.execute(
        "INSERT INTO experiments(created_at,kind,config_hash,window_from,window_to,summary,"
        "result_hash,contaminated) VALUES (?,?,?,?,?,?,?,?)",
        (
            fmt_ts(now),
            kind,
            cfg_hash,
            fmt_ts(start),
            fmt_ts(end),
            json.dumps(summary, sort_keys=True),
            res_hash,
            int(contaminated),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid, contaminated
