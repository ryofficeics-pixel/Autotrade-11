"""Event-driven replay of the live decision chain on stored history (no look-ahead).

At each tick the strategy sees only data observed at/before that tick (features, stored safety
verdicts and price cross-checks are all point-in-time). Decisions execute LATER: at the first
tick of that mint at/after decision time + latency, at that tick's price, worse by price impact
and a slippage buffer, with DEX fees, a network fee, a deterministic failed-transaction draw,
missed fills, stop gaps (exit at the next observed price, never at the stop), and dead tokens
(positions still open when a token stops printing are written down by a haircut).
Sizing reuses risk.sizing; momentum reuses strategies.hybrid.momentum_check.

Simplifications (documented limits): provider-health and kill-switch gates are not
reconstructable and are skipped; the live edge hypothesis gate is skipped (edge is what we
measure); fills use the DEX Screener snapshot price (no order-book path); one position per mint.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from autotrade.backtest.config import BacktestConfig
from autotrade.backtest.data import Tick, load_ticks, price_check_ok, safety_passed
from autotrade.config import AppConfig, RiskLimits, SignalPolicy
from autotrade.features.momentum import compute_features
from autotrade.paper.execution_model import FillParams, buy_fill, sell_fill, tx_fails
from autotrade.risk.sizing import compute_sizing
from autotrade.strategies.hybrid import momentum_check

ZERO, ONE = Decimal(0), Decimal(1)


@dataclass
class Trade:
    mint: str
    opened_at: datetime
    closed_at: datetime
    notional: Decimal  # USD spent on the token (before fees)
    qty: Decimal
    entry_mid: Decimal  # tick price at fill (no impact/slippage)
    entry_fill: Decimal
    exit_mid: Decimal
    exit_fill: Decimal
    fees: Decimal  # DEX fees + network fees, entry and exit
    net_pnl: Decimal
    exit_reason: str  # stop | liquidity | time | dead | open_at_end
    censored: bool = False  # still open at end of data, marked at last price

    @property
    def gross_mid_pnl(self) -> Decimal:
        return self.qty * (self.exit_mid - self.entry_mid)

    @property
    def net_return(self) -> Decimal:
        return self.net_pnl / self.notional


@dataclass
class _Pos:
    mint: str
    opened_at: datetime
    notional: Decimal
    qty: Decimal
    entry_mid: Decimal
    entry_fill: Decimal
    entry_cost: Decimal  # fees paid at entry
    stop: Decimal
    liq_floor: Decimal
    exit_after: datetime | None = None
    exit_reason: str = ""
    last_t: datetime | None = None
    last_price: Decimal = ZERO


@dataclass
class _Pending:
    mint: str
    decided_at: datetime
    exec_after: datetime
    expires: datetime
    notional: Decimal
    stop: Decimal
    liq_floor: Decimal


@dataclass
class Result:
    start: datetime
    end: datetime
    initial_equity: Decimal
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    exposure_samples: list[tuple[datetime, Decimal]] = field(default_factory=list)
    missed_fills: int = 0
    failed_tx: int = 0
    failed_tx_cost: Decimal = ZERO
    decisions: int = 0
    ticks: int = 0
    mints: int = 0
    benchmark_return: Decimal | None = None
    notes: list[str] = field(default_factory=list)


def simulate(
    conn: sqlite3.Connection,
    config: AppConfig,
    bt: BacktestConfig,
    start: datetime,
    end: datetime,
    mints: list[str] | None = None,
) -> Result:
    rk, sp = config.risk, config.signals
    fp = FillParams(
        rk.dex_fee_fraction, rk.network_fee_usd, bt.slippage_buffer, rk.max_price_impact_fraction
    )
    fx = rk.usd_idr_rate
    if fx is None:
        raise ValueError("backtest needs AUTOTRADE_USD_IDR_RATE (equity is IDR, prices are USD)")
    equity0 = config.initial_equity_idr / fx
    res = Result(start=start, end=end, initial_equity=equity0)
    ticks = load_ticks(conn, start, end, mints)
    res.ticks, res.mints = len(ticks), len({t.mint for t in ticks})
    cash = equity0
    open_pos: dict[str, _Pos] = {}
    pending: dict[str, _Pending] = {}
    last_tick: dict[str, Tick] = {}
    prev_t: dict[str, datetime] = {}
    cooldown_until: dict[str, datetime] = {}
    realized_by_day: dict[str, Decimal] = {}
    first_price: dict[str, Decimal] = {}

    def mtm() -> Decimal:
        return cash + sum((p.qty * p.last_price for p in open_pos.values()), ZERO)

    def exposure_usd() -> Decimal:
        return sum((p.qty * p.last_price for p in open_pos.values()), ZERO)

    def close(
        pos: _Pos,
        tick_t: datetime,
        mid: Decimal,
        liq: Decimal,
        reason: str,
        haircut: Decimal | None = None,
        censored: bool = False,
    ) -> None:
        nonlocal cash
        sell = sell_fill(mid, liq, pos.qty, fp, haircut=haircut)
        fill, proceeds, fee = sell.fill_price, sell.proceeds, sell.fee
        cash += proceeds - fee
        fees = pos.entry_cost + fee
        net = proceeds - fee - pos.notional - pos.entry_cost
        res.trades.append(
            Trade(
                pos.mint,
                pos.opened_at,
                tick_t,
                pos.notional,
                pos.qty,
                pos.entry_mid,
                pos.entry_fill,
                mid,
                fill,
                fees,
                net,
                reason,
                censored,
            )
        )
        day = tick_t.date().isoformat()
        realized_by_day[day] = realized_by_day.get(day, ZERO) + net
        cooldown_until[pos.mint] = tick_t + timedelta(seconds=rk.cooldown_seconds)
        del open_pos[pos.mint]

    for tick in ticks:
        t, m = tick.t, tick.mint
        first_price.setdefault(m, tick.price)
        gap = (t - prev_t[m]).total_seconds() if m in prev_t else 0
        prev_t[m] = t
        last_tick[m] = tick

        pos = open_pos.get(m)
        if pos is not None:
            pos.last_t, pos.last_price = t, tick.price
            if pos.exit_after is not None and t >= pos.exit_after:  # scheduled exit executes now
                close(pos, t, tick.price, tick.liquidity, pos.exit_reason)
            elif pos.exit_after is None:
                reason = _exit_reason(pos, tick, bt)
                if reason:
                    pos.exit_after = t + timedelta(seconds=bt.latency_seconds)
                    pos.exit_reason = reason
                    if bt.latency_seconds == 0:
                        close(pos, t, tick.price, tick.liquidity, reason)

        pend = pending.get(m)
        if pend is not None:
            if t > pend.expires:
                del pending[m]
                res.missed_fills += 1
            elif t >= pend.exec_after:
                del pending[m]
                cash = _try_enter(res, bt, rk, tick, pend, open_pos, cash)

        _sample(res, t, mtm(), exposure_usd())
        if m in open_pos or m in pending or gap > bt.max_data_gap_seconds:
            continue
        if cooldown_until.get(m, t) > t:
            continue
        if not (
            safety_passed(conn, m, t, sp.max_safety_age_seconds)
            and price_check_ok(conn, m, t, rk.provider_health_max_age_seconds)
        ):
            continue
        reasons, blockers = momentum_check(compute_features(conn, m, t).values, sp)
        if blockers:
            continue
        res.decisions += 1
        decision = _decide(
            res,
            rk,
            sp,
            bt,
            tick,
            cash,
            len(open_pos) + len(pending),
            exposure_usd(),
            mtm(),
            realized_by_day,
            equity0,
        )
        if decision is not None:
            pending[m] = decision

    # ---- end of window ------------------------------------------------------------------
    for pos in list(open_pos.values()):
        tick = last_tick[pos.mint]
        if (end - tick.t).total_seconds() >= bt.dead_after_seconds:
            close(pos, tick.t, tick.price, tick.liquidity, "dead", haircut=bt.dead_token_haircut)
        else:
            close(pos, tick.t, tick.price, tick.liquidity, "open_at_end", censored=True)
    res.missed_fills += len(pending)
    res.equity_curve.append((end, cash))
    rets = [last_tick[m].price / p - 1 for m, p in first_price.items()]
    res.benchmark_return = sum(rets, ZERO) / len(rets) if rets else None
    return res


def _exit_reason(pos: _Pos, tick: Tick, bt: BacktestConfig) -> str:
    if tick.price <= pos.stop:
        return "stop"
    if tick.liquidity <= pos.liq_floor:
        return "liquidity"
    if (tick.t - pos.opened_at).total_seconds() >= bt.max_hold_seconds:
        return "time"
    return ""


def _decide(
    res: Result,
    rk: RiskLimits,
    sp: SignalPolicy,
    bt: BacktestConfig,
    tick: Tick,
    cash: Decimal,
    n_open: int,
    exposure: Decimal,
    equity: Decimal,
    realized_by_day: dict[str, Decimal],
    equity0: Decimal,
) -> _Pending | None:
    day_loss = realized_by_day.get(tick.t.date().isoformat(), ZERO)
    if day_loss <= -rk.daily_loss_stop_fraction * equity0 or n_open >= rk.max_concurrent_positions:
        return None
    stop = tick.price * (ONE - sp.invalidation_price_drop)
    sz = compute_sizing(
        tick.price,
        stop,
        tick.liquidity,
        rk,
        equity_usd=equity,
        cash_usd=cash,
        exposure_usd=exposure,
    )
    if sz is None or sz.notional < rk.min_order_usd:
        return None
    if sz.fixed_fraction > rk.max_fixed_cost_fraction or sz.impact > min(
        rk.max_price_impact_fraction, rk.max_slippage_fraction
    ):
        return None
    if bt.apply_edge_gate:
        if rk.assumed_edge_fraction is None:
            return None
        if rk.assumed_edge_fraction <= sz.round_trip_cost + rk.edge_safety_margin_fraction:
            return None
    return _Pending(
        tick.mint,
        tick.t,
        tick.t + timedelta(seconds=bt.latency_seconds),
        tick.t + timedelta(seconds=bt.max_pending_seconds),
        sz.notional,
        stop,
        tick.liquidity * (ONE - sp.invalidation_liquidity_drop),
    )


def _try_enter(
    res: Result,
    bt: BacktestConfig,
    rk: RiskLimits,
    tick: Tick,
    pend: _Pending,
    open_pos: dict[str, _Pos],
    cash: Decimal,
) -> Decimal:
    cost_cash = pend.notional * (ONE + rk.dex_fee_fraction) + rk.network_fee_usd
    if cost_cash > cash:
        res.missed_fills += 1
        return cash
    if tx_fails(bt.seed, tick.mint, tick.t, "tx", bt.failed_tx_rate):
        res.failed_tx += 1
        res.failed_tx_cost += rk.network_fee_usd
        return cash - rk.network_fee_usd  # a failed transaction still pays the fee
    fp = FillParams(
        rk.dex_fee_fraction, rk.network_fee_usd, bt.slippage_buffer, rk.max_price_impact_fraction
    )
    buy = buy_fill(tick.price, tick.liquidity, pend.notional, fp)
    if buy is None or tick.price <= pend.stop:
        res.missed_fills += 1  # price moved against us / liquidity thinned before the fill
        return cash
    open_pos[tick.mint] = _Pos(
        tick.mint,
        tick.t,
        pend.notional,
        buy.qty,
        tick.price,
        buy.fill_price,
        buy.entry_cost,
        pend.stop,
        pend.liq_floor,
        last_t=tick.t,
        last_price=tick.price,
    )
    return cash - pend.notional - buy.entry_cost


def _sample(res: Result, t: datetime, equity: Decimal, exposure: Decimal) -> None:
    res.equity_curve.append((t, equity))
    res.exposure_samples.append((t, exposure / equity if equity > 0 else ZERO))
