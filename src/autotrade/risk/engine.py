"""Deterministic risk gate (M4): the final authority between a research signal and a PAPER intent.

Pure over (stored data, config, portfolio, clock): same inputs => same decision. Every rule is
evaluated and recorded (PASS/FAIL/UNKNOWN) so the decision is explainable and replayable.
Unknown critical input rejects. Nothing here talks to a network, broker or signer.

Sizing (docs/RISK_POLICY.md): N <= E*r / (d + c), with c = round-trip fees + price impact +
fixed network fees. With impact = 2N/L (constant-product approximation, L = pool liquidity USD)
the bound is the positive root of  (4/L) N^2 + (d + 2f) N + (2F - E r) = 0  (f = DEX fee per side,
F = network fee per transaction). N is then capped by the position cap, cash above the reserve,
remaining aggregate exposure and the liquidity/impact cap.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from autotrade.config import AppConfig
from autotrade.domain.models import RiskDecision, RuleResult, RuleStatus
from autotrade.domain.timeutil import fmt_ts, parse_ts
from autotrade.risk import state
from autotrade.risk.portfolio import PortfolioState
from autotrade.risk.sizing import compute_sizing

REQUIRED_PROVIDERS = ("solana_rpc", "dexscreener", "jupiter")
SNAPSHOT_SOURCE = "dexscreener"
ZERO = Decimal(0)


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


def _ok(cond: bool) -> RuleStatus:
    return RuleStatus.PASS if cond else RuleStatus.FAIL


def _unknown(rule: str, why: str) -> RuleResult:
    return _r(rule, RuleStatus.UNKNOWN, detail=why)


def idempotency_key(signal: sqlite3.Row) -> str:
    raw = f"{signal['mint']}|{signal['strategy_version']}|{signal['feature_as_of']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def evaluate_signal(
    conn: sqlite3.Connection,
    signal_id: int,
    now: datetime,
    config: AppConfig,
    portfolio: PortfolioState,
) -> RiskDecision:
    sig = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if sig is None:
        raise LookupError(f"signal {signal_id} not found")
    mint, rk = sig["mint"], config.risk
    key = idempotency_key(sig)
    rules: list[RuleResult] = []
    inputs: dict[str, Any] = {"signal_id": signal_id, "portfolio": _portfolio_json(portfolio)}

    # ---- gates independent of size -------------------------------------------------------
    rules.append(
        _r("mode_paper", _ok(config.mode == "paper" and not config.live_enabled), config.mode)
    )
    halted, why = state.status(conn)
    rules.append(_r("kill_switch", _ok(not halted), "on" if halted else "off", "off", why))
    expired = fmt_ts(now) >= sig["expires_at"]
    rules.append(_r("signal_unexpired", _ok(not expired), sig["expires_at"], f"> {fmt_ts(now)}"))
    rules.append(_safety_rule(conn, mint, now, config))
    rules.append(_provider_rule(conn, now, config))
    quote = _latest_quote(conn, mint)
    rules.append(_quote_fresh_rule(quote, now, rk.max_quote_age_seconds))
    price = Decimal(quote["price_usd"]) if quote and quote["price_usd"] is not None else None
    liq = Decimal(quote["liquidity_usd"]) if quote and quote["liquidity_usd"] else None
    if quote:
        inputs["snapshot_id"] = quote["id"]
    rules.append(_price_check_rule(conn, mint, now, rk.provider_health_max_age_seconds))

    # ---- portfolio gates ------------------------------------------------------------------
    rules += _portfolio_rules(conn, mint, now, config, portfolio, key)

    # ---- sizing and economics -------------------------------------------------------------
    size = _sizing_rules(rules, sig, price, liq, config, portfolio)
    first_fail = next((r for r in rules if r.status is not RuleStatus.PASS), None)
    if first_fail is not None or size is None:
        code = first_fail.rule.upper() if first_fail else "SIZING_UNAVAILABLE"
        return RiskDecision(
            signal_id=signal_id,
            mint=mint,
            decided_at=now,
            approved=False,
            idempotency_key=key,
            reason_code=code,
            rules=tuple(rules),
            inputs=inputs,
        )
    notional_usd, stop = size
    assert price is not None
    return RiskDecision(
        signal_id=signal_id,
        mint=mint,
        decided_at=now,
        approved=True,
        idempotency_key=key,
        reason_code="APPROVED",
        rules=tuple(rules),
        notional_usd=notional_usd,
        notional_idr=notional_usd * _fx(config),
        quantity=notional_usd / price,
        entry_price_usd=price,
        stop_price_usd=stop,
        inputs=inputs,
    )


def _fx(config: AppConfig) -> Decimal:
    fx = config.risk.usd_idr_rate
    assert fx is not None
    return fx


def _portfolio_json(p: PortfolioState) -> dict[str, str]:
    return {
        "equity_idr": str(p.equity_idr),
        "cash_idr": str(p.cash_idr),
        "start_of_day_equity_idr": str(p.start_of_day_equity_idr),
        "realized_pnl_today_idr": str(p.realized_pnl_today_idr),
        "positions": json.dumps([[x.mint, str(x.notional_idr)] for x in p.positions]),
    }


def _safety_rule(
    conn: sqlite3.Connection, mint: str, now: datetime, config: AppConfig
) -> RuleResult:
    ev = conn.execute(
        "SELECT * FROM safety_evaluations WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,)
    ).fetchone()
    if ev is None:
        return _unknown("safety_pass", "no safety evaluation")
    age = (now - parse_ts(ev["evaluated_at"])).total_seconds()
    limit = config.signals.max_safety_age_seconds
    return _r(
        "safety_pass",
        _ok(ev["verdict"] == "pass" and age <= limit),
        f"{ev['verdict']} {age:.0f}s old",
        f"pass, <= {limit}s",
    )


def _provider_rule(conn: sqlite3.Connection, now: datetime, config: AppConfig) -> RuleResult:
    p = config.providers
    required = [
        n
        for n, on in (
            ("solana_rpc", True),
            ("dexscreener", p.dexscreener_enabled),
            ("jupiter", p.jupiter_enabled),
        )
        if on
    ]
    limit = config.risk.provider_health_max_age_seconds
    bad: list[str] = []
    for name in required:
        row = conn.execute(
            "SELECT status, checked_at FROM provider_health WHERE provider=? ORDER BY id DESC "
            "LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            bad.append(f"{name}:no-health-record")
        elif row["status"] != "healthy":
            bad.append(f"{name}:{row['status']}")
        elif (now - parse_ts(row["checked_at"])).total_seconds() > limit:
            bad.append(f"{name}:stale-health")
    return _r(
        "providers_healthy",
        _ok(not bad),
        ",".join(bad) or "all healthy",
        f"healthy within {limit}s",
    )


def _latest_quote(conn: sqlite3.Connection, mint: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM market_snapshots WHERE base_mint=? AND source=? "
        "ORDER BY observed_at DESC, CAST(liquidity_usd AS REAL) DESC LIMIT 1",
        (mint, SNAPSHOT_SOURCE),
    ).fetchone()
    return row


def _quote_fresh_rule(quote: sqlite3.Row | None, now: datetime, limit: int) -> RuleResult:
    if quote is None:
        return _unknown("quote_fresh", "no DEX Screener snapshot")
    age = (now - parse_ts(quote["observed_at"])).total_seconds()
    complete = quote["price_usd"] is not None and quote["liquidity_usd"] is not None
    if not complete:
        return _unknown("quote_fresh", "quote lacks USD price or liquidity")
    return _r("quote_fresh", _ok(0 <= age <= limit), f"{age:.1f}s", f"<= {limit}s")


def _price_check_rule(conn: sqlite3.Connection, mint: str, now: datetime, limit: int) -> RuleResult:
    row = conn.execute(
        "SELECT * FROM cross_checks WHERE mint=? AND check_type='price_usd' ORDER BY id DESC "
        "LIMIT 1",
        (mint,),
    ).fetchone()
    if row is None or (now - parse_ts(row["checked_at"])).total_seconds() > limit:
        return _unknown("quote_consistent", "no recent price cross-check")
    if row["status"] == "unavailable":
        return _unknown("quote_consistent", row["detail"] or "price check unavailable")
    return _r("quote_consistent", _ok(row["status"] == "ok"), row["deviation"], row["detail"])


def _portfolio_rules(
    conn: sqlite3.Connection,
    mint: str,
    now: datetime,
    config: AppConfig,
    pf: PortfolioState,
    key: str,
) -> list[RuleResult]:
    rk = config.risk
    out: list[RuleResult] = []
    stop_level = rk.daily_loss_stop_fraction * pf.start_of_day_equity_idr
    out.append(
        _r(
            "daily_loss_stop",
            _ok(pf.realized_pnl_today_idr > -stop_level),
            pf.realized_pnl_today_idr,
            f"> -{stop_level}",
        )
    )
    out.append(
        _r(
            "max_positions",
            _ok(len(pf.positions) < rk.max_concurrent_positions),
            len(pf.positions),
            f"< {rk.max_concurrent_positions}",
        )
    )
    held = any(p.mint == mint for p in pf.positions)
    out.append(_r("no_averaging_down", _ok(not held), "held" if held else "flat", "flat"))
    dup = conn.execute(
        "SELECT 1 FROM risk_decisions WHERE idempotency_key=? AND approved=1", (key,)
    ).fetchone()
    out.append(_r("duplicate_intent", _ok(dup is None), "exists" if dup else "new", "new"))
    since = fmt_ts(now - timedelta(seconds=rk.cooldown_seconds))
    recent = conn.execute(
        "SELECT 1 FROM risk_decisions WHERE mint=? AND approved=1 AND decided_at > ?", (mint, since)
    ).fetchone()
    out.append(
        _r(
            "cooldown",
            _ok(recent is None),
            "recent approval" if recent else "none",
            f"none in {rk.cooldown_seconds}s",
        )
    )
    return out


def _sizing_rules(
    rules: list[RuleResult],
    sig: sqlite3.Row,
    price: Decimal | None,
    liq: Decimal | None,
    config: AppConfig,
    pf: PortfolioState,
) -> tuple[Decimal, Decimal] | None:
    rk = config.risk
    fx = rk.usd_idr_rate
    if fx is None:
        rules.append(_unknown("fx_rate", "AUTOTRADE_USD_IDR_RATE not configured"))
        return None
    rules.append(_r("fx_rate", RuleStatus.PASS, fx))
    inv = json.loads(sig["invalidation"])
    try:
        stop = Decimal(inv["price_usd_below"])
    except (KeyError, ArithmeticError, TypeError):
        rules.append(_unknown("stop_distance", "signal has no invalidation price"))
        return None
    if price is None or liq is None or liq <= 0:
        rules.append(_unknown("stop_distance", "no usable quote"))
        return None
    d = (price - stop) / price
    rules.append(
        _r(
            "stop_distance",
            _ok(ZERO < d < 1),
            f"{d:.4f}",
            "0 < d < 1",
            "price at/below the invalidation level" if d <= 0 else None,
        )
    )
    if not ZERO < d < 1:
        return None

    sz = compute_sizing(
        price,
        stop,
        liq,
        rk,
        equity_usd=pf.equity_idr / fx,
        cash_usd=pf.cash_idr / fx,
        exposure_usd=pf.exposure_idr / fx,
    )
    assert sz is not None  # stop distance and liquidity were validated above
    rules.append(_r("cash_reserve", _ok(sz.cash_free > 0), f"{sz.cash_free:.4f} USD free", "> 0"))
    rules.append(
        _r("exposure_room", _ok(sz.exposure_room > 0), f"{sz.exposure_room:.4f} USD", "> 0")
    )
    n, binding, fixed_frac, impact = sz.notional, sz.binding, sz.fixed_fraction, sz.impact
    if n <= 0:
        rules.append(_r("order_size", RuleStatus.FAIL, n, "> 0", f"binding cap: {binding}"))
        return None
    rules.append(
        _r(
            "order_size",
            _ok(n >= rk.min_order_usd),
            f"{n} USD",
            f">= {rk.min_order_usd}",
            f"binding cap: {binding}",
        )
    )
    rules.append(
        _r(
            "fixed_costs",
            _ok(fixed_frac <= rk.max_fixed_cost_fraction),
            f"{fixed_frac:.4f}",
            f"<= {rk.max_fixed_cost_fraction}",
            "network fees too large relative to order (uneconomic minimum)",
        )
    )
    rules.append(
        _r(
            "price_impact",
            _ok(impact <= rk.max_price_impact_fraction),
            f"{impact:.5f}",
            f"<= {rk.max_price_impact_fraction}",
        )
    )
    slippage = impact  # no separate quote-vs-fill model before M6; impact is the estimate
    rules.append(
        _r(
            "slippage",
            _ok(slippage <= rk.max_slippage_fraction),
            f"{slippage:.5f}",
            f"<= {rk.max_slippage_fraction}",
        )
    )
    cost = sz.round_trip_cost
    if rk.assumed_edge_fraction is None:
        rules.append(
            _unknown(
                "edge_vs_costs",
                "no validated edge: set AUTOTRADE_RISK_ASSUMED_"
                "EDGE_FRACTION as an explicit hypothesis (see M5/M8)",
            )
        )
    else:
        need = cost + rk.edge_safety_margin_fraction
        rules.append(
            _r(
                "edge_vs_costs",
                _ok(rk.assumed_edge_fraction > need),
                rk.assumed_edge_fraction,
                f"> {need:.5f} (costs {cost:.5f} + margin)",
            )
        )
    if any(r.status is not RuleStatus.PASS for r in rules):
        return None
    return n, stop


def decide_safely(
    conn: sqlite3.Connection,
    signal_id: int,
    now: datetime,
    config: AppConfig,
    portfolio: PortfolioState,
    log: Any = None,
) -> RiskDecision:
    """Engine exception => halt new entries (kill switch) and return a rejection; the exception
    is logged, never swallowed silently."""
    sig = conn.execute("SELECT mint FROM signals WHERE id=?", (signal_id,)).fetchone()
    if sig is None:
        raise LookupError(f"signal {signal_id} not found")  # caller bug, not an engine failure
    try:
        return evaluate_signal(conn, signal_id, now, config, portfolio)
    except Exception as exc:
        if log is not None:
            log.exception("risk engine failure; halting new entries")
        state.engage(conn, now, f"risk engine exception: {type(exc).__name__}")
        return RiskDecision(
            signal_id=signal_id,
            mint=sig["mint"],
            decided_at=now,
            approved=False,
            idempotency_key=f"error-{signal_id}",
            reason_code="ENGINE_ERROR",
            rules=(_r("engine_error", RuleStatus.FAIL, type(exc).__name__),),
        )
