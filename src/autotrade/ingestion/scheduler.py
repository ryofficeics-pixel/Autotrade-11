"""Bounded polling loop with graceful shutdown. Stops on event or max_cycles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from autotrade.domain.models import IngestionResult

from .engine import IngestionEngine
from .jobs import IngestJob


async def poll(
    engine: IngestionEngine,
    jobs: Sequence[IngestJob],
    interval_seconds: float,
    stop: asyncio.Event,
    max_cycles: int | None = None,
    after_cycle: Callable[[], Awaitable[None]] | None = None,
) -> list[list[IngestionResult]]:
    """Run jobs every interval until `stop` is set (or max_cycles). The current cycle always
    completes before shutdown; no cycle is started once stop is set."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    cycles: list[list[IngestionResult]] = []
    while not stop.is_set() and (max_cycles is None or len(cycles) < max_cycles):
        cycles.append(await engine.run_once(jobs))
        if after_cycle is not None:
            await after_cycle()
        if max_cycles is not None and len(cycles) >= max_cycles:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
    return cycles
