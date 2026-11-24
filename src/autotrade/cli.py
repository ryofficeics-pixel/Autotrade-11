"""Operator CLI. Read-only data tooling; no trading, signing or broadcast commands exist.

Exit codes: 0 ok | 1 check/ingestion failed or degraded | 2 usage/config error | 3 skipped
(offline mode or nothing configured).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from .backtest import registry as bt_registry
from .backtest import runner as bt_runner
from .backtest.config import BacktestConfig
from .config import AppConfig, load_dotenv
from .data.storage import repositories as repo
from .data.storage.db import Database
from .domain.models import (
    CrossCheck,
    HealthStatus,
    IngestionResult,
    ProviderHealth,
    RiskDecision,
    RunStatus,
    SafetyEvaluation,
    Signal,
    Verdict,
)
from .domain.timeutil import fmt_ts, parse_ts, utcnow
from .features.momentum import compute_features
from .ingestion.crosscheck import run_cross_checks
from .ingestion.engine import IngestionEngine
from .ingestion.freshness import assess
from .ingestion.jobs import IngestJob
from .ingestion.scheduler import poll
from .observability.logging import configure_logging, redact
from .paper import broker, ledger
from .paper.portfolio import load_portfolio
from .risk import state as risk_state
from .risk.engine import decide_safely
from .risk.portfolio import PortfolioState
from .runtime import build_database, build_discovery_job, build_jobs, build_providers
from .safety.screening import evaluate_and_store
from .strategies.hybrid import Decision, generate_signal

OK, FAIL, USAGE, SKIPPED = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autotrade", description="Autotrade-11 (PAPER-only; read-only data tooling)"
    )
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="validate configuration and environment")
    doctor.add_argument("--network", action="store_true", help="also probe the RPC endpoint")
    sub.add_parser("status", help="show mode, database and data freshness")
    db = sub.add_parser("db", help="database operations").add_subparsers(
        dest="db_command", required=True
    )
    db.add_parser("init", help="create/migrate the database (idempotent)")
    db.add_parser("check", help="run integrity and migration checks")
    prov = sub.add_parser("providers", help="provider operations").add_subparsers(
        dest="providers_command", required=True
    )
    prov.add_parser("health", help="probe providers (network)")
    cand = sub.add_parser("candidates", help="list discovered candidates and their latest verdict")
    cand.add_argument("--limit", type=int, default=20)
    saf = sub.add_parser("safety", help="evaluate safety rules on stored data (offline)")
    saf.add_argument("--mint", help="evaluate one mint instead of watchlist + candidates")
    saf.add_argument("-v", "--verbose", action="store_true", help="show per-rule evidence")
    risk = sub.add_parser("risk", help="risk gate (paper intents; kill switch)").add_subparsers(
        dest="risk_command", required=True
    )
    risk.add_parser("evaluate", help="run the risk gate on all active signals (offline)")
    risk.add_parser("decisions", help="list recent risk decisions").add_argument(
        "--limit", type=int, default=20
    )
    ks = risk.add_parser("kill-switch", help="engage/release/inspect the persistent halt")
    ks.add_argument("action", choices=["on", "off", "status"])
    ks.add_argument("--reason", default="manual")
    bt = sub.add_parser("backtest", help="replay stored history (no look-ahead)").add_subparsers(
        dest="backtest_command", required=True
    )
    lock = bt.add_parser("lock", help="fix the untouched holdout boundary (once)")
    lock.add_argument("--fraction", default="0.2", help="share of stored history to reserve")
    bt.add_parser("run", help="development replay (never reaches the holdout)")
    wf = bt.add_parser("walkforward", help="sequential out-of-sample folds on development data")
    wf.add_argument("--folds", type=int, default=4)
    bt.add_parser("holdout", help="evaluate the locked holdout (contamination is recorded)")
    bt.add_parser("list", help="list recorded experiments")
    paper = sub.add_parser("paper", help="paper broker and ledger (simulated; no orders sent)")
    pc = paper.add_subparsers(dest="paper_command", required=True)
    pc.add_parser("run", help="process pending paper orders/exits with stored quotes (offline)")
    pc.add_parser("status", help="cash, equity, positions, open orders")
    pc.add_parser("reconcile", help="verify ledger vs fills vs positions vs PnL")
    pc.add_parser("report", help="orders, rejected signals, missed/failed fills, PnL")
    sig = sub.add_parser("signals", help="list unexpired research signals (never orders)")
    sig.add_argument("--all", action="store_true", help="include expired signals")
    feat = sub.add_parser("features", help="compute point-in-time features from stored data")
    feat.add_argument("--mint", required=True)
    ing = sub.add_parser("ingest", help="ingest watchlist data into the local store")
    mode = ing.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="single cycle")
    mode.add_argument("--watch", action="store_true", help="poll until SIGINT/SIGTERM")
    return p


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return USAGE if exc.code else OK
    if env is None:
        load_dotenv(Path(".env"))
    try:
        config = AppConfig.from_env(env)
    except (ValidationError, ValueError) as exc:
        print(f"CONFIG ERROR: {redact(str(exc))}", file=sys.stderr)
        return USAGE
    configure_logging(config.data.log_level)
    handlers = {
        "doctor": _doctor,
        "status": _status,
        "db": _db,
        "providers": _providers,
        "ingest": _ingest,
        "candidates": _candidates,
        "safety": _safety,
        "signals": _signals,
        "paper": _paper,
        "backtest": _backtest,
        "risk": _risk,
        "features": _features,
    }
    return handlers[args.command](config, args)


def _doctor(config: AppConfig, args: argparse.Namespace) -> int:
    print("Configuration valid; execution mode=PAPER; live execution=LOCKED")
    problems = 0
    print(f"ok   python {sys.version_info.major}.{sys.version_info.minor}")
    print(f"ok   rpc endpoints: {len(config.providers.endpoints)} configured (host-only display)")
    db = build_database(config)
    if db.exists():
        with db.session(create=False) as conn:
            version = db.schema_version(conn)
        state = "up to date" if version == db.latest_version() else "needs `autotrade db init`"
        print(f"ok   database {config.data.db_path} schema v{version} ({state})")
    else:
        print(f"warn database {config.data.db_path} not initialized (run `autotrade db init`)")
    if config.data.offline:
        print("info offline mode: network checks skipped")
    elif args.network:
        for health in asyncio.run(_probe(config)):
            mark = "ok  " if health.status is HealthStatus.HEALTHY else "FAIL"
            print(f"{mark} {health.provider} {health.status.value}")
            problems += health.status is not HealthStatus.HEALTHY
    return FAIL if problems else OK


async def _probe(config: AppConfig) -> list[ProviderHealth]:
    """Probe every enabled provider (Birdeye is not probed: calls cost the user's credits)."""
    prov = build_providers(config)
    try:
        probes = [p.check_health() for p in (prov.rpc, prov.dex, prov.jupiter) if p is not None]
        out = list(await asyncio.gather(*probes))
        if prov.birdeye:
            out.append(prov.birdeye.health())
        return out
    finally:
        await prov.aclose()


def _status(config: AppConfig, args: argparse.Namespace) -> int:
    print(
        f"mode={config.mode} equity_idr={config.initial_equity_idr} "
        f"live_enabled={config.live_enabled}"
    )
    print(f"offline={config.data.offline} db={config.data.db_path}")
    db = build_database(config)
    if not db.exists():
        print("db: not initialized (run `autotrade db init`)")
        return OK
    now = utcnow()
    try:
        with db.session(create=False) as conn:
            print(f"db: schema v{db.schema_version(conn)}/{db.latest_version()}")
            for table, n in repo.table_counts(conn).items():
                print(f"  {table}: {n}")
            for table in ("market_snapshots", "wallet_activity"):
                latest = repo.latest_observed(conn, table)
                limit = (
                    config.data.max_snapshot_age_seconds if table == "market_snapshots" else None
                )
                if limit is None:
                    print(f"  latest {table}: {latest or 'none'}")
                    continue
                rep = assess(parse_ts(latest) if latest else None, now, limit)
                age = f"{rep.age_seconds:.0f}s" if rep.age_seconds is not None else "n/a"
                print(f"  freshness {table}: {rep.state.value} (age {age}, limit {limit:.0f}s)")
            for h in repo.latest_health(conn):
                print(
                    f"  provider {h['provider']}: {h['status']} "
                    f"failures={h['consecutive_failures']} checked_at={h['checked_at']}"
                )
            for r in repo.recent_runs(conn, 3):
                print(f"  run #{r['id']} {r['job']} {r['status']} started={r['started_at']}")
    except sqlite3.Error as exc:
        print(f"ERROR reading database: {redact(str(exc))}", file=sys.stderr)
        return FAIL
    return OK


def _db(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    try:
        if args.db_command == "init":
            with db.session() as conn:
                applied = db.migrate(conn)
                report = db.integrity_check(conn)
            print(f"migrations applied: {applied or 'none (already current)'}")
            print(f"database {config.data.db_path}: {'ok' if report.ok else 'PROBLEMS'}")
            for p in report.problems:
                print(f"  {p}")
            return OK if report.ok else FAIL
        if not db.exists():
            print("database not found (run `autotrade db init`)", file=sys.stderr)
            return FAIL
        with db.session(create=False) as conn:
            report = db.integrity_check(conn)
        print("integrity: ok" if report.ok else "integrity: PROBLEMS")
        for p in report.problems:
            print(f"  {p}")
        return OK if report.ok else FAIL
    except Exception as exc:  # report and fail with a code; type is shown, secrets redacted
        print(f"ERROR: {type(exc).__name__}: {redact(str(exc))}", file=sys.stderr)
        return FAIL


def _providers(config: AppConfig, args: argparse.Namespace) -> int:
    if config.data.offline:
        print("offline mode: provider probes skipped")
        return SKIPPED
    results = asyncio.run(_probe(config))
    db = build_database(config)
    for health in results:
        print(
            f"{health.provider} endpoint={health.endpoint} status={health.status.value} "
            f"latency_ms={health.latency_ms} failures={health.consecutive_failures}"
        )
        if health.last_error_kind:
            print(f"  last_error={health.last_error_kind}: {health.last_error_message}")
        if db.exists():
            with db.session(create=False) as conn:
                try:
                    repo.record_health(conn, health)
                except sqlite3.Error as exc:
                    print(f"warn could not record health: {redact(str(exc))}", file=sys.stderr)
    return OK if all(h.status is HealthStatus.HEALTHY for h in results) else FAIL


def _ingest(config: AppConfig, args: argparse.Namespace) -> int:
    if config.data.offline:
        print("offline mode: ingestion skipped")
        return SKIPPED
    d = config.data
    if not (d.watch_mints or d.watch_wallets or d.discovery_enabled):
        print("nothing to ingest: set AUTOTRADE_WATCH_MINTS/WALLETS or AUTOTRADE_DISCOVERY_ENABLED")
        return SKIPPED
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    with db.session(create=False) as conn:
        if db.schema_version(conn) != db.latest_version():
            print("database schema out of date (run `autotrade db init`)", file=sys.stderr)
            return FAIL
    return asyncio.run(_ingest_async(config, db, watch=args.watch))


async def _ingest_async(config: AppConfig, db: Database, *, watch: bool) -> int:
    prov = build_providers(config)
    engine = IngestionEngine(db, max_concurrency=config.data.max_concurrency)
    checks: list[CrossCheck] = []
    verdicts: list[SafetyEvaluation] = []
    decisions: list[Decision] = []
    risk_decisions: list[RiskDecision] = []
    paper_events: list[broker.Event] = []

    def current_mints() -> list[str]:
        with db.session(create=False) as conn:
            return _all_mints(config, conn)

    def jobs() -> list[IngestJob]:
        return build_jobs(config, prov, mints=current_mints(), pending_flows=pending_flows)

    def pending_flows() -> list[tuple[str, str]]:
        limit = config.signals.max_tx_parse_per_cycle
        if limit == 0:
            return []
        with db.session(create=False) as conn:
            return repo.unparsed_signatures(conn, limit)

    discovery = build_discovery_job(config, prov)
    discovery_results: list[IngestionResult] = []

    async def before_cycle() -> None:
        if discovery is not None:
            discovery_results.append(await engine.run_job(discovery))

    async def after_cycle() -> None:
        mints = current_mints()
        now, d, s = utcnow(), config.data, config.safety
        with db.session(create=False) as conn, db.transaction(conn):
            paper_events.extend(broker.run_cycle(conn, now, config))  # fill/exit earlier orders
            if not mints:
                _reconcile_or_halt(conn, now, config, paper_events)
                return
            checks[:] = run_cross_checks(
                conn,
                mints,
                now,
                max_age_seconds=d.max_snapshot_age_seconds,
                price_tolerance=d.price_tolerance_fraction,
            )
            verdicts[:] = evaluate_and_store(
                conn, mints, now, s, max_data_age_seconds=d.max_snapshot_age_seconds
            )
            decisions[:] = [generate_signal(conn, m, now, config.signals) for m in mints]
            risk_decisions[:] = _risk_gate(
                conn, [x.signal for x in decisions if x.signal], now, config
            )
            paper_events.extend(broker.run_cycle(conn, now, config))  # accept new intents
            _reconcile_or_halt(conn, now, config, paper_events)

    stop = asyncio.Event()
    if watch:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
                break
    try:
        cycles = await poll(
            engine,
            jobs,
            config.data.poll_interval_seconds,
            stop,
            max_cycles=None if watch else 1,
            after_cycle=after_cycle,
            before_cycle=before_cycle,
        )
    finally:
        await prov.aclose()
    failed = False
    cycles = [discovery_results, *cycles] if discovery_results else cycles
    for results in cycles:
        for r in results:
            print(
                f"{r.job}: {r.status.value} fetched={r.fetched} accepted={r.accepted} "
                f"rejected={r.rejected} inserted={r.inserted} updated={r.updated} "
                f"duplicates={r.duplicates} stale={r.stale}"
            )
            if r.error_kind:
                print(f"  error={r.error_kind}: {r.error_message}")
            for note in r.rejections[:5]:
                print(f"  rejected: {note}")
            failed |= r.status is not RunStatus.SUCCESS
    for c in checks:
        dev = f" deviation={c.deviation:.4f}" if c.deviation is not None else ""
        print(f"check {c.check_type} {c.mint[:6]}…: {c.status.value}{dev} {c.detail or ''}")
    _print_verdicts(verdicts)
    _print_decisions(decisions)
    _print_risk(risk_decisions)
    _print_paper(paper_events)
    return FAIL if failed else OK


def _all_mints(config: AppConfig, conn: sqlite3.Connection) -> list[str]:
    """Watchlist plus recent discovery candidates (bounded). Order: watchlist first."""
    s = config.safety
    since = utcnow() - timedelta(hours=s.max_candidate_age_hours)
    found = repo.candidate_mints(conn, since, s.max_candidates_per_cycle)
    held = [
        r[0]
        for r in conn.execute(
            "SELECT mint FROM paper_positions WHERE closed_at IS NULL ORDER BY id"
        )
    ]
    # open paper positions must keep being polled, or they could be mistaken for dead tokens
    return list(dict.fromkeys([*config.data.watch_mints, *held, *found]))


def _risk_gate(
    conn: sqlite3.Connection, signals: Sequence[Signal], now: datetime, config: AppConfig
) -> list[RiskDecision]:
    portfolio = load_portfolio(conn, now, config)
    out: list[RiskDecision] = []
    for s in signals:
        sid = repo.signal_id_for(conn, s)
        if sid is not None:
            out.append(_decide(conn, sid, now, config, portfolio))
    return out


def _decide(
    conn: sqlite3.Connection, signal_id: int, now: datetime, config: AppConfig, pf: PortfolioState
) -> RiskDecision:
    d = decide_safely(conn, signal_id, now, config, pf, log=logging.getLogger("autotrade.risk"))
    repo.insert_risk_decision(conn, d)
    return d


def _print_risk(decisions: Sequence[RiskDecision]) -> None:
    for d in decisions:
        if d.approved:
            print(
                f"risk APPROVED {d.mint[:8]}…: paper intent notional_usd={d.notional_usd} "
                f"qty={d.quantity} stop={d.stop_price_usd} (no order is placed in M4)"
            )
        else:
            fails = [f"{r.rule}:{r.status.value}" for r in d.rules if r.status.value != "pass"]
            print(f"risk REJECTED {d.mint[:8]}…: {d.reason_code} [{', '.join(fails)}]")


def _risk(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    now = utcnow()
    with db.session(create=False) as conn, db.transaction(conn):
        if args.risk_command == "kill-switch":
            if args.action == "on":
                risk_state.engage(conn, now, args.reason)
            elif args.action == "off":
                risk_state.release(conn, now)
            halted, why = risk_state.status(conn)
            print(f"kill switch: {'ON' if halted else 'off'}" + (f" ({why})" if why else ""))
            return OK
        if args.risk_command == "decisions":
            rows = conn.execute(
                "SELECT * FROM risk_decisions ORDER BY id DESC LIMIT ?", (args.limit,)
            ).fetchall()
            for r in rows:
                print(
                    f"#{r['id']} {r['decided_at']} {r['mint'][:8]}… "
                    f"{'APPROVED' if r['approved'] else 'REJECTED'} {r['reason_code']} "
                    f"notional_usd={r['notional_usd']}"
                )
            if not rows:
                print("no risk decisions")
            return OK
        ids = [r["id"] for r in repo.active_signals(conn, now)]
        pf = load_portfolio(conn, now, config)
        decisions = [_decide(conn, i, now, config, pf) for i in ids]
    if not decisions:
        print("no active signals to evaluate")
        return SKIPPED
    _print_risk(decisions)
    return OK if all(d.approved for d in decisions) else FAIL


def _paper(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    now = utcnow()
    with db.session(create=False) as conn, db.transaction(conn):
        cmd = args.paper_command
        if cmd == "run":
            events: list[broker.Event] = broker.run_cycle(conn, now, config)
            _print_paper(events)
            _reconcile_or_halt(conn, now, config, events)
            print(f"{len(events)} paper event(s)")
            return OK
        if not conn.execute("SELECT 1 FROM ledger_entries WHERE kind='deposit'").fetchone():
            print("paper ledger not funded yet (needs AUTOTRADE_USD_IDR_RATE and one ingest/run)")
            return SKIPPED
        if cmd == "reconcile":
            problems = ledger.reconcile(conn)
            print(
                "ledger reconciles: cash, quantities, fills and PnL agree"
                if not problems
                else "LEDGER MISMATCH:"
            )
            for p in problems:
                print(f"  {p}")
            return OK if not problems else FAIL
        if cmd == "status":
            _print_paper_status(conn)
        else:
            _print_paper_report(conn)
    return OK


def _print_paper_status(conn: sqlite3.Connection) -> None:
    eq, cash, prices = ledger.equity(conn)
    print(f"cash_usd={cash:.6f} equity_usd={eq:.6f}")
    for p in ledger.open_positions(conn):
        mark = prices.get(p["mint"])
        print(
            f"  open {p['mint'][:8]}… qty={p['qty']} cost_usd={p['cost_basis_usd']} "
            f"mark={'n/a' if mark is None else mark} stop={p['stop_usd']} opened={p['opened_at']}"
        )
    pending = conn.execute("SELECT COUNT(*) n FROM paper_orders WHERE status='pending'").fetchone()
    print(f"pending orders: {pending['n']}")


def _print_paper_report(conn: sqlite3.Connection) -> None:
    _print_paper_status(conn)
    print(
        "orders by status:",
        dict(conn.execute("SELECT status, COUNT(*) FROM paper_orders GROUP BY status").fetchall()),
    )
    for r in conn.execute(
        "SELECT kind, status, status_reason, COUNT(*) n FROM paper_orders "
        "WHERE status IN ('missed','failed') GROUP BY kind, status, status_reason"
    ):
        print(f"  {r['status']} {r['kind']}: {r['status_reason']} x{r['n']}")
    print(
        "rejected signals by reason:",
        dict(
            conn.execute(
                "SELECT reason_code, COUNT(*) FROM risk_decisions "
                "WHERE approved=0 GROUP BY reason_code"
            ).fetchall()
        ),
    )
    closed = conn.execute(
        "SELECT exit_reason, COUNT(*) n, SUM(CAST(realized_pnl_usd AS REAL)) pnl "
        "FROM paper_positions WHERE closed_at IS NOT NULL GROUP BY exit_reason"
    ).fetchall()
    for r in closed:
        print(f"  closed via {r['exit_reason']}: n={r['n']} realized_pnl_usd~{r['pnl']:.6f}")
    problems = ledger.reconcile(conn)
    print("reconciliation:", "ok" if not problems else f"MISMATCH {problems}")


def _reconcile_or_halt(
    conn: sqlite3.Connection, now: datetime, config: AppConfig, events: list[broker.Event]
) -> None:
    if config.risk.usd_idr_rate is None:
        return
    problems = ledger.reconcile(conn)
    if problems:  # circuit breaker: a ledger that does not reconcile stops new entries
        risk_state.engage(conn, now, "ledger mismatch: " + "; ".join(problems)[:200])
        print("LEDGER MISMATCH (kill switch engaged): " + "; ".join(problems), file=sys.stderr)


def _print_paper(events: Sequence[broker.Event]) -> None:
    for e in events:
        print(f"paper {e.kind} #{e.order_id} {e.mint[:8]}…: {e.outcome} {e.detail}")


def _print_decisions(decisions: Sequence[Decision]) -> None:
    for d in decisions:
        tag = "SIGNAL" if d.signal else "no-signal"
        print(f"{tag} {d.mint[:8]}…: {', '.join(d.reasons)}")
        if d.signal:
            print(
                f"  expires={d.signal.expires_at.isoformat()} score={d.signal.score} "
                f"(research output only; places no orders)"
            )


def _print_verdicts(evals: Sequence[SafetyEvaluation]) -> None:
    for e in evals:
        why = f" [{', '.join(e.reasons)}]" if e.reasons else ""
        print(f"safety {e.mint[:8]}…: {e.verdict.value.upper()}{why} (ruleset {e.ruleset_version})")


def _backtest(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    bt, now = BacktestConfig(), utcnow()
    try:
        with db.session(create=False) as conn, db.transaction(conn):
            cmd = args.backtest_command
            if cmd == "lock":
                start, created = bt_registry.lock_holdout(conn, now, Decimal(args.fraction))
                print(
                    f"holdout starts {start.isoformat()} "
                    f"({'locked now' if created else 'already locked; unchanged'})"
                )
                return OK
            if cmd == "list":
                for r in conn.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT 20"):
                    print(
                        f"#{r['id']} {r['created_at']} {r['kind']} {r['window_from']}.."
                        f"{r['window_to']} hash={r['result_hash'][:12]}"
                        f"{' CONTAMINATED' if r['contaminated'] else ''}"
                    )
                return OK
            if cmd == "run":
                report = bt_runner.dev_run(conn, config, bt, now)
            elif cmd == "walkforward":
                report = bt_runner.walk_forward(conn, config, bt, now, args.folds)
            else:
                report = bt_runner.holdout_run(conn, config, bt, now)
    except ValueError as exc:
        print(f"backtest: {exc}", file=sys.stderr)
        return FAIL
    _print_report(report)
    return OK


def _print_report(r: bt_runner.Report) -> None:
    print(
        f"backtest {r.kind} #{r.experiment_id} window {r.window[0].isoformat()} .. "
        f"{r.window[1].isoformat()} result_hash={r.result_hash[:16]}"
    )
    for note in r.notes:
        print(f"WARNING: {note}")
    for k, v in r.summary.items():
        print(f"  {k}: {v}")
    for f in r.folds:
        print(
            f"  fold {f['fold']}: trades={f['trades']} net_pnl_usd={f['net_pnl_usd']} "
            f"expectancy={f['expectancy_return_per_trade']}"
        )
    print("  (research only: past replay is not evidence of future returns)")


def _signals(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    with db.session(create=False) as conn:
        now = utcnow()
        rows = (
            conn.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 50").fetchall()
            if args.all
            else repo.active_signals(conn, now)
        )
    for r in rows:
        live = "ACTIVE" if r["expires_at"] > fmt_ts(now) else "EXPIRED"
        print(
            f"{live} {r['mint']} {r['strategy_version']} score={r['score']} "
            f"created={r['created_at']} expires={r['expires_at']} reasons={r['reasons']}"
        )
    if not rows:
        print("no active signals")
    return OK


def _features(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    with db.session(create=False) as conn:
        fs = compute_features(conn, args.mint, utcnow())
    for k, v in sorted(fs.values.items()):
        print(f"{k}: {'unknown' if v is None else v}")
    for n in fs.notes:
        print(f"note: {n}")
    return OK


def _candidates(config: AppConfig, args: argparse.Namespace) -> int:
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    with db.session(create=False) as conn:
        rows = conn.execute(
            "SELECT * FROM candidates ORDER BY first_discovered_at DESC LIMIT ?", (args.limit,)
        ).fetchall()
        for r in rows:
            ev = repo.latest_evaluation(conn, r["mint"])
            verdict = ev["verdict"].upper() if ev else "NOT EVALUATED"
            print(
                f"{r['mint']} first_seen={r['first_discovered_at']} seen={r['times_seen']} "
                f"verdict={verdict}"
            )
    if not rows:
        print("no candidates (enable AUTOTRADE_DISCOVERY_ENABLED and run `autotrade ingest`)")
    return OK


def _safety(config: AppConfig, args: argparse.Namespace) -> int:
    """Re-evaluate stored data offline; no network. Exit 1 if any mint is rejected."""
    db = build_database(config)
    if not db.exists():
        print("database not initialized (run `autotrade db init`)", file=sys.stderr)
        return FAIL
    with db.session(create=False) as conn, db.transaction(conn):
        mints = [args.mint] if args.mint else _all_mints(config, conn)
        if not mints:
            print("nothing to evaluate")
            return SKIPPED
        evals = evaluate_and_store(
            conn,
            mints,
            utcnow(),
            config.safety,
            max_data_age_seconds=config.data.max_snapshot_age_seconds,
        )
    _print_verdicts(evals)
    if args.verbose:
        for e in evals:
            for r in e.rules:
                print(
                    f"  {e.mint[:8]}… {r.rule}: {r.status.value} observed={r.observed} "
                    f"threshold={r.threshold}"
                )
    return OK if all(e.verdict is Verdict.PASS for e in evals) else FAIL


if __name__ == "__main__":
    sys.exit(main())
