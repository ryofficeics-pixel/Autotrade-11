"""Deterministic token safety screening (M2). Produces a verdict + per-rule evidence.

A PASS means "no rule failed on fresh, verified data". It is not a trade signal, not a guarantee
of safety (no honeypot simulation, no Token-2022 extension analysis), and creates no orders.
Any missing, stale or unverifiable critical input yields UNKNOWN, which rejects.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from autotrade.config import SafetyPolicy
from autotrade.domain.models import (
    RuleResult,
    RuleStatus,
    SafetyEvaluation,
    Verdict,
)
from autotrade.domain.timeutil import parse_ts

RULESET_VERSION = "m2-1"
CLASSIC_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SNAPSHOT_SOURCE = "dexscreener"
INTEL_SOURCE = "jupiter"


def _r(
    rule: str,
    status: RuleStatus,
    observed: object = None,
    threshold: object = None,
    detail: str | None = None,
) -> RuleResult:
    return RuleResult(
        rule=rule,
        status=status,
        observed=None if observed is None else str(observed),
        threshold=None if threshold is None else str(threshold),
        detail=detail,
    )


def _unknown(rule: str, why: str) -> RuleResult:
    return _r(rule, RuleStatus.UNKNOWN, detail=why)


def _age(ts: str, now: datetime) -> float:
    return (now - parse_ts(ts)).total_seconds()


def evaluate_mint(
    conn: sqlite3.Connection,
    mint: str,
    now: datetime,
    policy: SafetyPolicy,
    *,
    max_data_age_seconds: float,
) -> SafetyEvaluation:
    token = conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()
    snaps = _latest_snapshots(conn, mint)
    intel = conn.execute(
        "SELECT * FROM token_intel WHERE mint=? AND source=? ORDER BY observed_at DESC LIMIT 1",
        (mint, INTEL_SOURCE),
    ).fetchone()
    checks = {
        r["check_type"]: r
        for r in conn.execute("SELECT * FROM cross_checks WHERE mint=? ORDER BY id ASC", (mint,))
    }  # later rows overwrite earlier: latest per type
    snap_fresh = bool(snaps) and _age(snaps[0]["observed_at"], now) <= max_data_age_seconds
    best = max(snaps, key=lambda s: Decimal(s["liquidity_usd"] or "0")) if snaps else None

    rules = [
        *_onchain_rules(token, policy),
        _data_fresh(snaps, snap_fresh, max_data_age_seconds, now),
        _liquidity(best, snap_fresh, policy),
        _pool_age(snaps, snap_fresh, policy, now),
        _concentration(intel, policy, now, max_data_age_seconds),
        _sellability(best, snap_fresh, policy),
        *_check_rules(checks, now, max_data_age_seconds),
    ]
    verdict = Verdict.PASS if all(r.status is RuleStatus.PASS for r in rules) else Verdict.REJECT
    inputs: dict[str, Any] = {
        "token_observed_at": token["observed_at"] if token else None,
        "snapshot_ids": [s["id"] for s in snaps],
        "intel_id": intel["id"] if intel else None,
        "cross_check_ids": {k: v["id"] for k, v in checks.items()},
        "policy": policy.model_dump(mode="json"),
    }
    return SafetyEvaluation(
        mint=mint,
        ruleset_version=RULESET_VERSION,
        evaluated_at=now,
        verdict=verdict,
        rules=tuple(rules),
        inputs=inputs,
    )


def _latest_snapshots(conn: sqlite3.Connection, mint: str) -> list[sqlite3.Row]:
    """All pools of the newest collection batch for this mint."""
    latest = conn.execute(
        "SELECT MAX(observed_at) FROM market_snapshots WHERE base_mint=? AND source=?",
        (mint, SNAPSHOT_SOURCE),
    ).fetchone()[0]
    if latest is None:
        return []
    return list(
        conn.execute(
            "SELECT s.*, p.created_at AS pool_created FROM market_snapshots s "
            "JOIN pools p ON p.address = s.pool_address "
            "WHERE s.base_mint=? AND s.source=? AND s.observed_at=?",
            (mint, SNAPSHOT_SOURCE, latest),
        )
    )


def _onchain_rules(token: sqlite3.Row | None, policy: SafetyPolicy) -> list[RuleResult]:
    names = (
        "onchain_record",
        "token_program",
        "mint_authority_revoked",
        "freeze_authority_revoked",
    )
    if token is None or not token["authorities_known"]:
        return [_unknown(n, "no verified on-chain (RPC) token record") for n in names]
    program = token["token_program"]
    if program is None:
        prog = _unknown("token_program", "token program unknown")
    elif program == CLASSIC_TOKEN_PROGRAM or policy.allow_token_2022:
        prog = _r("token_program", RuleStatus.PASS, program)
    else:
        prog = _r(
            "token_program",
            RuleStatus.FAIL,
            program,
            CLASSIC_TOKEN_PROGRAM,
            "non-classic token program (e.g. Token-2022 extensions) not analysed",
        )
    return [
        _r("onchain_record", RuleStatus.PASS, token["observed_at"]),
        prog,
        _r(
            "mint_authority_revoked",
            RuleStatus.PASS if token["mint_authority"] is None else RuleStatus.FAIL,
            token["mint_authority"] or "revoked",
        ),
        _r(
            "freeze_authority_revoked",
            RuleStatus.PASS if token["freeze_authority"] is None else RuleStatus.FAIL,
            token["freeze_authority"] or "revoked",
        ),
    ]


def _data_fresh(snaps: list[Any], fresh: bool, limit: float, now: datetime) -> RuleResult:
    if not snaps:
        return _unknown("market_data_fresh", "no DEX Screener snapshot")
    age = _age(snaps[0]["observed_at"], now)
    return _r(
        "market_data_fresh",
        RuleStatus.PASS if fresh else RuleStatus.FAIL,
        f"{age:.0f}s",
        f"<= {limit:.0f}s",
    )


def _liquidity(best: Any, fresh: bool, policy: SafetyPolicy) -> RuleResult:
    if best is None or not fresh or best["liquidity_usd"] is None:
        return _unknown("liquidity", "no fresh reported liquidity")
    value = Decimal(best["liquidity_usd"])
    ok = value >= policy.min_liquidity_usd
    return _r(
        "liquidity",
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        value,
        f">= {policy.min_liquidity_usd}",
    )


def _pool_age(snaps: list[Any], fresh: bool, policy: SafetyPolicy, now: datetime) -> RuleResult:
    created = [s["pool_created"] for s in snaps if s["pool_created"]]
    if not created or not fresh:
        return _unknown("pool_age", "pair creation time not reported")
    minutes = _age(min(created), now) / 60
    ok = minutes >= policy.min_pool_age_minutes
    return _r(
        "pool_age",
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        f"{minutes:.0f}min",
        f">= {policy.min_pool_age_minutes}min",
    )


def _concentration(intel: Any, policy: SafetyPolicy, now: datetime, limit: float) -> RuleResult:
    if intel is None or intel["top_holders_pct"] is None:
        return _unknown("holder_concentration", "Jupiter top-holder data unavailable")
    if _age(intel["observed_at"], now) > limit:
        return _unknown("holder_concentration", "Jupiter data stale")
    value = Decimal(intel["top_holders_pct"])
    ok = value <= policy.max_top_holders_pct
    return _r(
        "holder_concentration",
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        f"{value:.2f}%",
        f"<= {policy.max_top_holders_pct}%",
        "provider-reported; unit inferred as percent",
    )


def _sellability(best: Any, fresh: bool, policy: SafetyPolicy) -> RuleResult:
    cols = ("sells_24h", "buys_24h", "sells_1h")
    if best is None or not fresh or any(best[c] is None for c in cols):
        return _unknown("sellability_evidence", "transaction counts not reported")
    sells24, buys24, sells1 = best["sells_24h"], best["buys_24h"], best["sells_1h"]
    ratio = Decimal(sells24) / Decimal(max(buys24, 1))
    ok = (
        sells24 >= policy.min_sells_24h
        and sells1 >= policy.min_sells_1h
        and ratio >= policy.min_sell_buy_ratio_24h
    )
    return _r(
        "sellability_evidence",
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        f"sells24h={sells24} sells1h={sells1} sell/buy={ratio:.2f}",
        f"sells24h>={policy.min_sells_24h}, sells1h>={policy.min_sells_1h}, "
        f"ratio>={policy.min_sell_buy_ratio_24h}",
        "observed sells are evidence of sellability, not proof",
    )


def _check_rules(checks: dict[str, Any], now: datetime, limit: float) -> list[RuleResult]:
    price = checks.get("price_usd")
    if price is None or _age(price["checked_at"], now) > limit:
        price_rule = _unknown("price_cross_check", "no recent DEX Screener vs Jupiter price check")
    elif price["status"] == "ok":
        price_rule = _r("price_cross_check", RuleStatus.PASS, price["deviation"])
    elif price["status"] == "discrepancy":
        price_rule = _r(
            "price_cross_check", RuleStatus.FAIL, price["deviation"], detail="providers disagree"
        )
    else:
        price_rule = _unknown("price_cross_check", price["detail"] or "price check unavailable")
    bad = [
        k
        for k in ("decimals", "mint_authority", "freeze_authority")
        if k in checks and checks[k]["status"] == "discrepancy"
    ]
    # 'unavailable' is acceptable here: RPC is already the source of truth for these facts.
    onchain = _r(
        "provider_vs_onchain",
        RuleStatus.FAIL if bad else RuleStatus.PASS,
        ",".join(bad) or "no discrepancy",
        detail="provider claims contradict on-chain data" if bad else None,
    )
    return [price_rule, onchain]


def evaluate_and_store(
    conn: sqlite3.Connection,
    mints: list[str] | tuple[str, ...],
    now: datetime,
    policy: SafetyPolicy,
    *,
    max_data_age_seconds: float,
) -> list[SafetyEvaluation]:
    """Evaluate each mint and persist the evaluation (append-only audit trail)."""
    from autotrade.data.storage import repositories as repo

    out = []
    for mint in mints:
        ev = evaluate_mint(conn, mint, now, policy, max_data_age_seconds=max_data_age_seconds)
        repo.insert_evaluation(conn, ev)
        out.append(ev)
    return out
