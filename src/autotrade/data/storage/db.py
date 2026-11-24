"""SQLite connection management, transactions, migrations and integrity checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from autotrade.domain.timeutil import Clock, fmt_ts, utcnow

from .migrations import MIGRATIONS, Migration


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)


_BOOTSTRAP = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)"""


class Database:
    def __init__(
        self,
        path: str | Path,
        clock: Clock = utcnow,
        migrations: tuple[Migration, ...] = MIGRATIONS,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._migrations = migrations

    def exists(self) -> bool:
        return self.path == ":memory:" or Path(self.path).is_file()

    def connect(self, *, create: bool = True) -> sqlite3.Connection:
        if self.path != ":memory:":
            p = Path(self.path)
            if create:
                p.parent.mkdir(parents=True, exist_ok=True)
            elif not p.is_file():
                raise FileNotFoundError(f"database not found: {self.path}")
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def session(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect(create=create)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    @contextmanager
    def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """All-or-nothing unit of work; any exception rolls everything back and re-raises."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ---- migrations -----------------------------------------------------------------
    def applied_versions(self, conn: sqlite3.Connection) -> dict[int, str]:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not has:
            return {}
        return {
            r["version"]: r["checksum"]
            for r in conn.execute("SELECT version, checksum FROM schema_migrations")
        }

    def migrate(self, conn: sqlite3.Connection) -> list[int]:
        """Apply pending migrations in order. Idempotent. Refuses drifted/unknown history."""
        conn.execute(_BOOTSTRAP)
        applied = self.applied_versions(conn)
        known = {m.version: m for m in self._migrations}
        for version, checksum in applied.items():
            if version not in known:
                raise MigrationError(f"database has unknown migration {version} (newer code?)")
            if known[version].checksum != checksum:
                raise MigrationError(f"checksum mismatch for applied migration {version}")
        done: list[int] = []
        for m in sorted(self._migrations, key=lambda m: m.version):
            if m.version in applied:
                continue
            with self.transaction(conn):
                for stmt in m.statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, checksum, applied_at)"
                    " VALUES (?,?,?,?)",
                    (m.version, m.name, m.checksum, fmt_ts(self._clock())),
                )
            done.append(m.version)
        return done

    def schema_version(self, conn: sqlite3.Connection) -> int:
        applied = self.applied_versions(conn)
        return max(applied) if applied else 0

    def latest_version(self) -> int:
        return max(m.version for m in self._migrations)

    # ---- integrity ------------------------------------------------------------------
    def integrity_check(self, conn: sqlite3.Connection) -> IntegrityReport:
        problems: list[str] = []
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if rows != ["ok"]:
            problems += [f"integrity_check: {r}" for r in rows]
        for r in conn.execute("PRAGMA foreign_key_check"):
            problems.append(f"foreign_key_check: table={r[0]} rowid={r[1]} parent={r[2]}")
        try:
            applied = self.applied_versions(conn)
        except sqlite3.DatabaseError as exc:
            applied = {}
            problems.append(f"schema_migrations unreadable: {exc}")
        known = {m.version: m for m in self._migrations}
        for version in sorted(set(known) - set(applied)):
            problems.append(f"migration {version} not applied")
        for version, checksum in applied.items():
            if version not in known:
                problems.append(f"unknown applied migration {version}")
            elif known[version].checksum != checksum:
                problems.append(f"checksum mismatch for migration {version}")
        return IntegrityReport(ok=not problems, problems=problems)
