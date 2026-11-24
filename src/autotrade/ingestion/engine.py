"""Controlled ingestion: one run record per job, all-or-nothing persistence, health tracking.

Ingestion only reads providers and writes the local store. It has no trading side effects.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from autotrade.data.storage import repositories as repo
from autotrade.data.storage.db import Database
from autotrade.domain.errors import ProviderError
from autotrade.domain.models import IngestionResult, RunStatus
from autotrade.domain.timeutil import Clock, utcnow
from autotrade.observability.logging import log_event, redact

from .jobs import IngestJob

log = logging.getLogger("autotrade.ingestion")
MAX_REJECTION_NOTES = 20


class IngestionEngine:
    def __init__(self, db: Database, *, clock: Clock = utcnow, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._db, self._clock, self._max_concurrency = db, clock, max_concurrency

    async def run_once(self, jobs: Sequence[IngestJob]) -> list[IngestionResult]:
        return [await self.run_job(job) for job in jobs]

    async def run_job(self, job: IngestJob) -> IngestionResult:
        started = self._clock()
        with self._db.session() as conn:
            run_id = repo.start_run(conn, job.name, job.provider.name, started)
        sem = asyncio.Semaphore(self._max_concurrency)
        try:
            batch = await job.fetch(sem)
        except BaseException as exc:  # incl. cancellation: record, then re-raise (never swallow)
            self._finish(
                run_id,
                job,
                started,
                RunStatus.FAILED,
                error=(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal",
                    redact(str(exc)),
                ),
            )
            raise

        counts, db_error = repo.Counts(), None
        if batch.items:
            try:
                with self._db.session() as conn, self._db.transaction(conn):
                    counts = job.persist(conn, batch)
            except sqlite3.Error as exc:
                db_error = ("database", redact(f"{type(exc).__name__}: {exc}"))
                log_event(log, "persist_failed_rolled_back", logging.ERROR, job=job.name)

        accepted = 0 if db_error else len(batch.items) - counts.conflicts
        rejections = list(batch.rejections)
        rejections += (
            [f"{counts.conflicts} record(s) conflict with stored immutable data"]
            if counts.conflicts
            else []
        )
        problems = bool(batch.errors or rejections)
        if (
            db_error
            or (batch.targets and len(batch.errors) == batch.targets)
            or (problems and accepted == 0)
        ):
            status = RunStatus.FAILED
        elif problems:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.SUCCESS
        error = db_error or self._summarize(batch.errors)
        result = self._finish(
            run_id,
            job,
            started,
            status,
            error=error,
            fetched=batch.fetched,
            accepted=accepted,
            rejected=len(rejections) if rejections else 0,
            counts=counts,
            stale=batch.stale,
            rejections=rejections,
        )
        with self._db.session() as conn:
            repo.record_health(conn, job.provider.health())
        log_event(
            log,
            "ingestion_run",
            job=job.name,
            status=status.value,
            accepted=accepted,
            inserted=result.inserted,
            duplicates=result.duplicates,
            stale=result.stale,
        )
        return result

    @staticmethod
    def _summarize(errors: list[ProviderError]) -> tuple[str, str] | None:
        if not errors:
            return None
        first = errors[0]
        return first.kind.value, redact(f"{len(errors)} target(s) failed; first: {first.message}")

    def _finish(
        self,
        run_id: int,
        job: IngestJob,
        started: datetime,
        status: RunStatus,
        *,
        error: tuple[str, str] | None = None,
        fetched: int = 0,
        accepted: int = 0,
        rejected: int = 0,
        counts: repo.Counts | None = None,
        stale: int = 0,
        rejections: list[str] | None = None,
    ) -> IngestionResult:
        counts = counts or repo.Counts()
        result = IngestionResult(
            job=job.name,
            provider=job.provider.name,
            status=status,
            started_at=started,
            finished_at=self._clock(),
            fetched=fetched,
            accepted=accepted,
            rejected=rejected,
            inserted=counts.inserted,
            updated=counts.updated,
            duplicates=counts.duplicates,
            stale=stale,
            error_kind=error[0] if error else None,
            error_message=error[1] if error else None,
            rejections=tuple((rejections or [])[:MAX_REJECTION_NOTES]),
            run_id=run_id,
        )
        with self._db.session() as conn:
            repo.finish_run(conn, run_id, result)
        return result
