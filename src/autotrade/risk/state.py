"""Persistent halt/kill switch. Survives restarts; only a human clears it."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from autotrade.domain.timeutil import fmt_ts

KEY = "halt"


def engage(conn: sqlite3.Connection, now: datetime, reason: str) -> None:
    conn.execute(
        "INSERT INTO risk_state(key,value,reason,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, reason=excluded.reason, "
        "updated_at=excluded.updated_at",
        (KEY, "on", reason, fmt_ts(now)),
    )


def release(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "INSERT INTO risk_state(key,value,reason,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, reason=excluded.reason, "
        "updated_at=excluded.updated_at",
        (KEY, "off", "released manually", fmt_ts(now)),
    )


def status(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """(halted, reason). No row = never engaged = running. Any other value halts (fail closed)."""
    row = conn.execute("SELECT value, reason FROM risk_state WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return False, None
    return row["value"] != "off", row["reason"]
