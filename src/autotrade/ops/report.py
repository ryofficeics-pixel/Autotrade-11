"""Daily markdown report from the local store (UTC day). Facts only; no recommendations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from autotrade.config import AppConfig
from autotrade.domain.timeutil import fmt_ts
from autotrade.ops.health import collect_issues
from autotrade.paper import ledger


def _bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return fmt_ts(start), fmt_ts(start + timedelta(days=1))


def _table(rows: list[sqlite3.Row], headers: list[str]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(r[i]) for i in range(len(headers))) + " |" for r in rows]
    return [*out, ""]


def _q(conn: sqlite3.Connection, sql: str, *args: str) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def build_daily_report(
    conn: sqlite3.Connection, day: date, now: datetime, config: AppConfig
) -> str:
    lo, hi = _bounds(day)
    lines = [
        f"# Autotrade-11 daily report {day.isoformat()} (UTC)",
        "",
        f"Generated {fmt_ts(now)}. Mode: PAPER. Live trading: LOCKED. Research only.",
        "",
    ]

    lines += ["## Health", ""]
    issues = collect_issues(conn, now, config)
    lines += [f"- **{i.severity.upper()}** {i.key}: {i.message}" for i in issues] or ["- no issues"]

    lines += ["", "## Ingestion runs", ""]
    lines += _table(
        _q(
            conn,
            "SELECT job, status, COUNT(*) n, SUM(inserted) inserted, SUM(rejected) "
            "rejected FROM ingestion_runs WHERE started_at >= ? AND started_at < ? "
            "GROUP BY job, status ORDER BY job, status",
            lo,
            hi,
        ),
        ["job", "status", "runs", "inserted", "rejected"],
    )
    lines += ["## Providers (latest)", ""]
    lines += _table(
        _q(
            conn,
            "SELECT h.provider, h.status, h.consecutive_failures, h.checked_at FROM "
            "provider_health h JOIN (SELECT provider, MAX(id) mid FROM provider_health "
            "GROUP BY provider) m ON h.id=m.mid ORDER BY h.provider",
        ),
        ["provider", "status", "failures", "checked_at"],
    )

    lines += ["## Discovery and screening", ""]
    cand = _q(
        conn,
        "SELECT COUNT(*) n FROM candidates WHERE first_discovered_at >= ? AND "
        "first_discovered_at < ?",
        lo,
        hi,
    )[0]["n"]
    lines.append(f"- new candidates: {cand}")
    lines += _table(
        _q(
            conn,
            "SELECT verdict, COUNT(*) n FROM safety_evaluations WHERE evaluated_at >= ? "
            "AND evaluated_at < ? GROUP BY verdict",
            lo,
            hi,
        ),
        ["verdict", "evaluations"],
    )

    lines += ["## Signals and risk decisions", ""]
    sigs = _q(
        conn, "SELECT COUNT(*) n FROM signals WHERE created_at >= ? AND created_at < ?", lo, hi
    )
    lines.append(f"- signals created: {sigs[0]['n']}")
    lines += _table(
        _q(
            conn,
            "SELECT approved, reason_code, COUNT(*) n FROM risk_decisions WHERE "
            "decided_at >= ? AND decided_at < ? GROUP BY approved, reason_code "
            "ORDER BY approved DESC, n DESC",
            lo,
            hi,
        ),
        ["approved", "reason", "decisions"],
    )

    lines += ["## Paper trading", ""]
    if conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone():
        eq, cash, _ = ledger.equity(conn)
        lines.append(
            f"- cash_usd={cash:.6f} equity_usd={eq:.6f} open_positions="
            f"{len(ledger.open_positions(conn))}"
        )
    else:
        lines.append("- ledger not funded")
    lines += _table(
        _q(
            conn,
            "SELECT kind, status, COALESCE(status_reason,''), COUNT(*) n FROM "
            "paper_orders WHERE created_at >= ? AND created_at < ? GROUP BY kind, "
            "status, status_reason",
            lo,
            hi,
        ),
        ["kind", "status", "reason", "orders"],
    )
    lines += ["### Closed positions", ""]
    lines += _table(
        _q(
            conn,
            "SELECT mint, exit_reason, realized_pnl_usd, closed_at FROM paper_positions "
            "WHERE closed_at >= ? AND closed_at < ? ORDER BY closed_at",
            lo,
            hi,
        ),
        ["mint", "exit", "realized_pnl_usd", "closed_at"],
    )
    problems = (
        ledger.reconcile(conn)
        if conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone()
        else []
    )
    lines.append(f"- reconciliation: {'ok' if not problems else 'MISMATCH ' + '; '.join(problems)}")

    lines += ["", "## Alerts", ""]
    lines += _table(
        _q(
            conn,
            "SELECT ts, severity, key, delivered FROM alerts WHERE ts >= ? AND ts < ? "
            "AND suppressed=0 ORDER BY ts",
            lo,
            hi,
        ),
        ["ts", "severity", "key", "delivered"],
    )
    return "\n".join(lines) + "\n"
