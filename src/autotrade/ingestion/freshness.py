"""Explicit freshness classification. Absent data is UNKNOWN, never 'fresh'."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessReport:
    state: Freshness
    age_seconds: float | None


def assess(observed_at: datetime | None, now: datetime, max_age_seconds: float) -> FreshnessReport:
    if observed_at is None:
        return FreshnessReport(Freshness.UNKNOWN, None)
    age = (now - observed_at).total_seconds()
    if age < -60:  # observation from the future beyond tolerated skew: untrustworthy
        return FreshnessReport(Freshness.UNKNOWN, age)
    return FreshnessReport(Freshness.STALE if age > max_age_seconds else Freshness.FRESH, age)
