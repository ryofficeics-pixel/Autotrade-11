"""Consistent online SQLite backups (sqlite3 backup API), verified, with retention."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class BackupError(RuntimeError):
    pass


def backup(db_path: Path, out_dir: Path, now: datetime, keep: int) -> Path:
    if not db_path.is_file():
        raise BackupError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"autotrade-{now.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)  # safe while other connections write (WAL)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(target)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        target.unlink(missing_ok=True)
        raise BackupError(f"backup failed integrity check: {result}")
    for old in sorted(out_dir.glob("autotrade-*.sqlite3"))[:-keep]:
        old.unlink()
    return target


def restore_instructions(backup_file: Path, db_path: Path) -> str:
    return (
        f"Stop the service, then: cp {backup_file} {db_path} && "
        f"rm -f {db_path}-wal {db_path}-shm && autotrade db check"
    )
