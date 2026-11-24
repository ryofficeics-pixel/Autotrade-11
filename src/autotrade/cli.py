"""Operator CLI. Read-only data tooling; no trading, signing or broadcast commands exist.

Exit codes: 0 ok | 1 check/ingestion failed or degraded | 2 usage/config error | 3 skipped
(offline mode or nothing configured).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from .config import AppConfig, load_dotenv
from .data.storage import repositories as repo
from .data.storage.db import Database
from .domain.models import (
    CrossCheck,
    HealthStatus,
    IngestionResult,
    ProviderHealth,
    RunStatus,
    SafetyEvaluation,
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
        if not mints:
            return
        now, d, s = utcnow(), config.data, config.safety
        with db.session(create=False) as conn, db.transaction(conn):
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
    return FAIL if failed else OK


def _all_mints(config: AppConfig, conn: sqlite3.Connection) -> list[str]:
    """Watchlist plus recent discovery candidates (bounded). Order: watchlist first."""
    s = config.safety
    since = utcnow() - timedelta(hours=s.max_candidate_age_hours)
    found = repo.candidate_mints(conn, since, s.max_candidates_per_cycle)
    return list(dict.fromkeys([*config.data.watch_mints, *found]))


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
