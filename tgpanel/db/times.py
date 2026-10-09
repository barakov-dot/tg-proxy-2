"""UTC time <-> TEXT/INTEGER conversion for SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def to_db(dt: datetime) -> str:
    """Fixed-width UTC ISO string (sorts lexicographically)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use UTC-aware values")
    return dt.astimezone(UTC).strftime(_FMT)


def to_db_opt(dt: datetime | None) -> str | None:
    return None if dt is None else to_db(dt)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, _FMT).replace(tzinfo=UTC)


def from_db_opt(value: str | None) -> datetime | None:
    return None if value is None else from_db(value)


def to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use UTC-aware values")
    return int(dt.timestamp())


def from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)
