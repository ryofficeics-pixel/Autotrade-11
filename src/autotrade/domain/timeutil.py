"""UTC timestamp helpers. Naive datetimes are rejected, never guessed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]
_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: object) -> datetime:
    """Normalize an aware datetime or ISO-8601 string (with offset) to UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be an aware datetime or ISO-8601 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive timestamp rejected; timezone is required")
    return value.astimezone(UTC)


def from_unix(seconds: int | float) -> datetime:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise ValueError("unix timestamp must be a number")
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"unix timestamp out of range: {seconds!r}") from exc


def fmt_ts(value: datetime) -> str:
    """Fixed-width UTC text; lexicographic order equals chronological order."""
    return to_utc(value).strftime(_FMT)


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _FMT).replace(tzinfo=UTC)
