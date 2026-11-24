"""Alerts to free, local sinks: an append-only file, and an optional user-provided webhook.

Each alert key is rate limited (cooldown) so a persistent fault does not spam. Delivery failures
are recorded and logged; they never raise into the trading/ingestion cycle. Messages are
redacted (no URLs, no keys). The webhook URL is a secret and is never logged or stored."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from autotrade.config import OpsSettings
from autotrade.data.providers.http import ResilientHttp, RetryPolicy
from autotrade.domain.errors import ProviderError
from autotrade.domain.timeutil import fmt_ts
from autotrade.observability.logging import log_event, redact

log = logging.getLogger("autotrade.alerts")
SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str


def _write_file(path: Path, now: datetime, a: Alert) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"ts": fmt_ts(now), "severity": a.severity, "key": a.key, "message": a.message}
            )
            + "\n"
        )


def _post_webhook(url: str, text: str) -> None:
    async def go() -> None:
        http = ResilientHttp(
            RetryPolicy(
                timeout_seconds=5,
                max_attempts=2,
                backoff_base_seconds=0.5,
                backoff_max_seconds=2,
                max_requests_per_second=2,
            )
        )
        try:
            await http.post_json(url, {"text": text})
        finally:
            await http.aclose()

    # Run in a worker thread: emit() is also called from inside the ingestion event loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, go()).result(timeout=30)


def emit(conn: sqlite3.Connection, now: datetime, ops: OpsSettings, alert: Alert) -> bool:
    """Deliver (or suppress) one alert. Returns True if it was delivered."""
    if alert.severity not in SEVERITIES:
        raise ValueError(f"unknown severity {alert.severity!r}")
    message = redact(alert.message)[:500]
    alert = Alert(alert.key, alert.severity, message)
    since = fmt_ts(now - timedelta(seconds=ops.alert_cooldown_seconds))
    recent = conn.execute(
        "SELECT 1 FROM alerts WHERE key=? AND suppressed=0 AND ts > ? LIMIT 1", (alert.key, since)
    ).fetchone()
    if recent:
        conn.execute(
            "INSERT INTO alerts(ts,key,severity,message,delivered,suppressed) VALUES (?,?,?,?,?,1)",
            (fmt_ts(now), alert.key, alert.severity, message, "suppressed:cooldown"),
        )
        return False
    delivered: list[str] = []
    try:
        _write_file(ops.alert_file, now, alert)
        delivered.append("file")
    except OSError as exc:
        log_event(log, "alert_file_failed", logging.ERROR, error=type(exc).__name__)
    if ops.alert_webhook_url is not None:
        try:
            _post_webhook(
                ops.alert_webhook_url.get_secret_value(),
                f"[{alert.severity}] {alert.key}: {message}",
            )
            delivered.append("webhook")
        except (ProviderError, RuntimeError, TimeoutError) as exc:
            log_event(log, "alert_webhook_failed", logging.ERROR, error=type(exc).__name__)
    log_event(log, "alert", logging.WARNING, key=alert.key, severity=alert.severity)
    conn.execute(
        "INSERT INTO alerts(ts,key,severity,message,delivered,suppressed) VALUES (?,?,?,?,?,0)",
        (fmt_ts(now), alert.key, alert.severity, message, ",".join(delivered) or "none"),
    )
    return True
