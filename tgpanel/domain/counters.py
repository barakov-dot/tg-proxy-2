"""Traffic counter delta logic, activity, buckets and retention (PLAN 3.5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ACTIVITY_MIN_BYTES = 2048
ACTIVITY_MIN_PACKETS = 10


@dataclass(frozen=True, slots=True)
class Counter:
    """One direction of one nft set element."""

    bytes: int
    packets: int


@dataclass(frozen=True, slots=True)
class Delta:
    bytes: int
    packets: int
    state: Counter  # new state to store
    reset: bool = False


def compute_delta(prev: Counter | None, current: Counter) -> Delta:
    """Delta since the previous poll.

    No previous state or a decreasing counter (reboot, rules reload) means the current
    value is the new base and the delta is 0. Never negative.
    """
    if prev is None:
        return Delta(0, 0, current, reset=False)
    if current.bytes < prev.bytes or current.packets < prev.packets:
        return Delta(0, 0, current, reset=True)
    return Delta(current.bytes - prev.bytes, current.packets - prev.packets, current)


def is_active(
    delta_bytes: int,
    delta_packets: int,
    *,
    min_bytes: int = ACTIVITY_MIN_BYTES,
    min_packets: int = ACTIVITY_MIN_PACKETS,
) -> bool:
    """Real activity: strictly more than the thresholds on both bytes and packets."""
    return delta_bytes > min_bytes and delta_packets > min_packets


def is_online(recent_activity: Sequence[bool]) -> bool:
    """Online = active in each of the last two polls (oldest first)."""
    return len(recent_activity) >= 2 and recent_activity[-1] and recent_activity[-2]


def floor_minute(ts: datetime) -> datetime:
    return _utc(ts).replace(second=0, microsecond=0)


def floor_hour(ts: datetime) -> datetime:
    return _utc(ts).replace(minute=0, second=0, microsecond=0)


def floor_day(ts: datetime) -> datetime:
    return _utc(ts).replace(hour=0, minute=0, second=0, microsecond=0)


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return ts.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    minute_days: int = 14
    hour_days: int = 180
    # day buckets are kept forever


@dataclass(frozen=True, slots=True)
class RollupCutoffs:
    minute_before: datetime  # minute buckets older than this are rolled into hours
    hour_before: datetime  # hour buckets older than this are rolled into days


DEFAULT_RETENTION = RetentionPolicy()


def rollup_cutoffs(now: datetime, policy: RetentionPolicy = DEFAULT_RETENTION) -> RollupCutoffs:
    return RollupCutoffs(
        minute_before=floor_hour(now - timedelta(days=policy.minute_days)),
        hour_before=floor_day(now - timedelta(days=policy.hour_days)),
    )


def choose_granularity(
    start: datetime,
    end: datetime,
    now: datetime,
    policy: RetentionPolicy = DEFAULT_RETENTION,
    max_points: int = 1500,
) -> str:
    """Finest granularity ('minute' | 'hour' | 'day') that is retained and not too dense."""
    span = max(end - start, timedelta(0))
    if start >= now - timedelta(days=policy.minute_days) and span <= timedelta(minutes=max_points):
        return "minute"
    if start >= now - timedelta(days=policy.hour_days) and span <= timedelta(hours=max_points):
        return "hour"
    return "day"
