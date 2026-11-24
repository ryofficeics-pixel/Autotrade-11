"""Operational health: one function used by the CLI health check, the alerter and the report."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from autotrade.config import AppConfig
from autotrade.domain.timeutil import parse_ts
from autotrade.ingestion.freshness import Freshness, assess
from autotrade.ops.alerts import Alert
from autotrade.paper import ledger
from autotrade.risk import state


@dataclass(frozen=True)
class Issue:
    key: str
    severity: str  # warning | critical
    message: str

    def as_alert(self) -> Alert:
        return Alert(self.key, self.severity, self.message)


def collect_issues(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Issue]:
    issues: list[Issue] = []
    halted, why = state.status(conn)
    if halted:
        issues.append(Issue("kill_switch", "critical", f"new entries halted: {why}"))

    last = conn.execute(
        "SELECT finished_at FROM ingestion_runs WHERE status IN ('success','partial') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last is None or last["finished_at"] is None:
        issues.append(Issue("ingest_never_ran", "warning", "no successful ingestion run recorded"))
    else:
        age = (now - parse_ts(last["finished_at"])).total_seconds()
        if age > config.ops.healthcheck_max_run_age_seconds:
            issues.append(
                Issue(
                    "ingest_stale",
                    "critical",
                    f"last successful ingestion {age:.0f}s ago "
                    f"(limit {config.ops.healthcheck_max_run_age_seconds}s)",
                )
            )

    for h in conn.execute(
        "SELECT h.provider, h.status, h.last_error_kind FROM provider_health h JOIN "
        "(SELECT provider, MAX(id) mid FROM provider_health GROUP BY provider) m ON h.id=m.mid"
    ):
        if h["status"] in ("down", "degraded"):
            sev = "critical" if h["status"] == "down" else "warning"
            issues.append(
                Issue(
                    f"provider_{h['provider']}",
                    sev,
                    f"{h['provider']} is {h['status']} ({h['last_error_kind']})",
                )
            )

    snap = conn.execute(
        "SELECT MAX(observed_at) t FROM market_snapshots WHERE source='dexscreener'"
    ).fetchone()["t"]
    if snap is not None:
        rep = assess(parse_ts(snap), now, max(config.data.max_snapshot_age_seconds * 4, 300))
        if rep.state is not Freshness.FRESH:
            issues.append(
                Issue(
                    "market_data_stale",
                    "critical",
                    f"newest market snapshot is {rep.state.value} (age {rep.age_seconds})",
                )
            )

    funded = conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone()
    if funded:
        for p in ledger.reconcile(conn):
            issues.append(Issue("ledger_mismatch", "critical", p))

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        issues.append(Issue("db_integrity", "critical", f"integrity_check: {integrity}"))
    return issues
