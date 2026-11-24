"""Strategy hybrid-v1: discovery -> safety PASS -> momentum confirmation (+ wallet context).

Emits expiring, reason-coded RESEARCH signals only. It has no access to any broker, order or
position code: a signal is a row in `signals` that the (future) risk engine must accept.
Unknown inputs never produce a signal.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from autotrade.config import SignalPolicy
from autotrade.data.storage import repositories as repo
from autotrade.domain.models import Signal
from autotrade.domain.timeutil import parse_ts
from autotrade.features.momentum import compute_features

STRATEGY_VERSION = "hybrid-v1"


@dataclass(frozen=True)
class Decision:
    mint: str
    signal: Signal | None
    reasons: tuple[str, ...]  # why no signal, or the reason codes of the signal


def _dec(values: dict[str, str | None], key: str) -> Decimal | None:
    v = values.get(key)
    return None if v is None else Decimal(v)


def generate_signal(
    conn: sqlite3.Connection, mint: str, now: datetime, policy: SignalPolicy
) -> Decision:
    ev = repo.latest_evaluation(conn, mint)
    if ev is None:
        return Decision(mint, None, ("NO_SAFETY_EVALUATION",))
    if ev["verdict"] != "pass":
        return Decision(mint, None, ("SAFETY_NOT_PASS",))
    if (now - parse_ts(ev["evaluated_at"])).total_seconds() > policy.max_safety_age_seconds:
        return Decision(mint, None, ("SAFETY_STALE",))

    feats = compute_features(conn, mint, now)
    repo.insert_feature_set(conn, feats)
    v = feats.values
    reasons: list[str] = []
    unknown: list[str] = []
    checks: list[tuple[str, str, bool | None]] = []

    def check(code: str, feature: str, test: Callable[[Decimal], bool]) -> None:
        value = _dec(v, feature)
        if value is None:
            unknown.append(f"UNKNOWN:{feature}")
            return
        checks.append((code, feature, test(value)))

    check("MOMENTUM_15M", "ret_15m", lambda x: x >= policy.min_ret_15m)
    check("MOMENTUM_5M_UP", "ret_5m", lambda x: x > 0)
    check("BUY_PRESSURE_5M", "imbalance_5m", lambda x: x >= policy.min_imbalance_5m)
    check("ACTIVITY_ACCELERATING", "txn_rate_accel", lambda x: x >= policy.min_txn_rate_accel)
    check("LIQUIDITY_STABLE", "liq_drawdown_1h", lambda x: x <= policy.max_liq_drawdown_1h)
    if unknown:
        return Decision(mint, None, tuple(unknown))
    failed = [f"FAILED:{c}" for c, _, ok in checks if not ok]
    if failed:
        return Decision(mint, None, tuple(failed))
    reasons = [c for c, _, _ in checks]
    if (_dec(v, "wallet_buyers_1h") or Decimal(0)) > 0:
        reasons.append("WALLET_BUYERS")  # optional context, never required

    price, liq = _dec(v, "price_usd"), _dec(v, "liquidity_usd")
    if price is None or liq is None:
        return Decision(mint, None, ("UNKNOWN:price_or_liquidity",))
    signal = Signal(
        mint=mint,
        strategy_version=STRATEGY_VERSION,
        created_at=now,
        expires_at=now + timedelta(seconds=policy.ttl_seconds),
        score=len(reasons),
        reasons=tuple(reasons),
        invalidation={
            "safety_verdict": "must_remain_pass",
            "price_usd_below": str(price * (1 - policy.invalidation_price_drop)),
            "liquidity_usd_below": str(liq * (1 - policy.invalidation_liquidity_drop)),
        },
        feature_as_of=feats.as_of,
        safety_evaluation_id=int(ev["id"]),
    )
    repo.insert_signal(conn, signal)
    return Decision(mint, signal, tuple(reasons))
