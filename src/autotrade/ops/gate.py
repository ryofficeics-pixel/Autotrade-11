"""Paper-to-live readiness report (docs/ACCEPTANCE_CRITERIA.md "Future live release gate").

It reports evidence; it NEVER enables anything. Live trading stays locked in code regardless of
the result, and item 10 (explicit user authorisation) is always NOT MET here."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from autotrade.config import AppConfig
from autotrade.ops.health import collect_issues

ZERO = Decimal(0)


class Gate(StrEnum):
    MET = "MET"
    NOT_MET = "NOT_MET"
    UNKNOWN = "UNKNOWN"  # needs data or a value only you can provide
    MANUAL = "MANUAL"  # cannot be automated; needs a human/independent review


@dataclass(frozen=True)
class Item:
    name: str
    status: Gate
    evidence: str
    evidence_item: bool = True  # False: human review / authorisation, never automatable


def consecutive_days(conn: sqlite3.Connection, today: date) -> int:
    """Trailing run of UTC days with at least one successful ingestion run (today may be
    incomplete, so a missing today is tolerated; any other missing day ends the run)."""
    days = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT substr(started_at,1,10) FROM ingestion_runs "
            "WHERE status IN ('success','partial')"
        )
    }
    cursor = today if today.isoformat() in days else today - timedelta(days=1)
    n = 0
    while cursor.isoformat() in days:
        n += 1
        cursor -= timedelta(days=1)
    return n


def trade_returns(conn: sqlite3.Connection) -> list[Decimal]:
    return [
        Decimal(r["realized_pnl_usd"]) / Decimal(r["cost_basis_usd"])
        for r in conn.execute(
            "SELECT realized_pnl_usd, cost_basis_usd FROM paper_positions "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at"
        )
    ]


def lower_bound(returns: list[Decimal], z: Decimal) -> tuple[Decimal, Decimal] | None:
    """(mean, one-sided lower confidence bound of the mean). Normal approximation; needs n>=2."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns, ZERO) / n
    var = sum(((r - mean) ** 2 for r in returns), ZERO) / (n - 1)
    se = (var / n).sqrt()
    return mean, mean - z * se


def realized_max_drawdown(conn: sqlite3.Connection) -> Decimal | None:
    dep = conn.execute("SELECT amount_usd FROM ledger_entries WHERE kind='deposit'").fetchone()
    if dep is None:
        return None
    equity = Decimal(dep["amount_usd"])
    events = [
        (r["closed_at"], Decimal(r["realized_pnl_usd"]))
        for r in conn.execute(
            "SELECT closed_at, realized_pnl_usd FROM paper_positions WHERE closed_at IS NOT NULL"
        )
    ] + [
        (r["ts"], Decimal(r["amount_usd"]))
        for r in conn.execute(
            "SELECT ts, amount_usd FROM ledger_entries WHERE kind='failed_tx_fee'"
        )
    ]
    peak, worst = equity, ZERO
    for _, pnl in sorted(events):
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak if peak > 0 else ZERO)
    return worst


def evaluate(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> list[Item]:
    g = config.gate
    items: list[Item] = []

    def add(name: str, status: Gate, evidence: str, evidence_item: bool = True) -> None:
        items.append(Item(name, status, evidence, evidence_item))

    days = consecutive_days(conn, now.date())
    add(
        f"{g.min_days} consecutive paper days",
        Gate.MET if days >= g.min_days else Gate.NOT_MET,
        f"{days} consecutive UTC day(s) with successful ingestion",
    )

    mism = conn.execute(
        "SELECT COUNT(*) n FROM alerts WHERE key='ledger_mismatch' AND suppressed=0"
    ).fetchone()["n"]
    funded = conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone()
    add(
        "zero ledger mismatches",
        Gate.MET if funded and mism == 0 else (Gate.NOT_MET if mism else Gate.UNKNOWN),
        f"{mism} ledger-mismatch alert(s) recorded" if funded else "paper ledger not funded",
    )

    rets = trade_returns(conn)
    add(
        f"minimum sample ({g.min_trades} closed paper trades)",
        Gate.MET if len(rets) >= g.min_trades else Gate.NOT_MET,
        f"{len(rets)} closed trades",
    )

    lb = lower_bound(rets, g.confidence_z)
    if lb is None or len(rets) < g.min_trades:
        add(
            "positive net expectancy (paper, after costs)",
            Gate.UNKNOWN,
            "not enough closed trades for the pre-registered test",
        )
    else:
        mean, low = lb
        add(
            "positive net expectancy (paper, after costs)",
            Gate.MET if low > 0 else Gate.NOT_MET,
            f"mean {mean:.4f} per trade, lower bound {low:.4f} (z={g.confidence_z})",
        )

    hold = conn.execute(
        "SELECT summary, contaminated FROM experiments "
        "WHERE kind='holdout' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if hold is None:
        add("positive expectancy on the untouched holdout", Gate.UNKNOWN, "no holdout run recorded")
    else:
        s = json.loads(hold["summary"])
        exp = s.get("expectancy_return_per_trade")
        n = int(s.get("trades", 0))
        if hold["contaminated"]:
            add(
                "positive expectancy on the untouched holdout",
                Gate.NOT_MET,
                "holdout is CONTAMINATED (evaluated under more than one configuration)",
            )
        elif exp is None or n < g.min_holdout_trades:
            add(
                "positive expectancy on the untouched holdout",
                Gate.UNKNOWN,
                f"{n} holdout trades (< {g.min_holdout_trades})",
            )
        else:
            add(
                "positive expectancy on the untouched holdout",
                Gate.MET if Decimal(exp) > 0 else Gate.NOT_MET,
                f"expectancy {exp} per trade over {n} trades",
            )

    dd = realized_max_drawdown(conn)
    if g.max_drawdown_fraction is None:
        add(
            "drawdown within approved budget",
            Gate.UNKNOWN,
            "no approved budget set (AUTOTRADE_GATE_MAX_DRAWDOWN)",
        )
    elif dd is None:
        add("drawdown within approved budget", Gate.UNKNOWN, "paper ledger not funded")
    else:
        add(
            "drawdown within approved budget",
            Gate.MET if dd <= g.max_drawdown_fraction else Gate.NOT_MET,
            f"realized max drawdown {dd:.4f} vs budget {g.max_drawdown_fraction}",
        )

    critical = [i for i in collect_issues(conn, now, config) if i.severity == "critical"]
    add(
        "no unresolved critical operational/reconciliation issue",
        Gate.MET if not critical else Gate.NOT_MET,
        "none" if not critical else "; ".join(f"{i.key}" for i in critical),
    )

    add(
        "signer and transaction integration independently reviewed",
        Gate.MANUAL,
        "no signer or transaction code exists in this repository (by design)",
        False,
    )
    add(
        "fee and loss budget explicitly approved",
        Gate.UNKNOWN if g.approved_loss_budget_usd is None else Gate.MET,
        "set AUTOTRADE_GATE_LOSS_BUDGET_USD to record your approval"
        if g.approved_loss_budget_usd is None
        else f"approved loss budget {g.approved_loss_budget_usd} USD",
    )
    add(
        "explicit user authorisation of live mode",
        Gate.NOT_MET,
        "live mode is locked in code; enabling it requires a separate reviewed change",
        False,
    )
    return items
