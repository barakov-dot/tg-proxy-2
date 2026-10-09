from datetime import UTC, datetime, timedelta, timezone

from tests.domain.helpers import NOW
from tgpanel.domain.counters import (
    DEFAULT_RETENTION,
    Counter,
    RetentionPolicy,
    choose_granularity,
    compute_delta,
    floor_day,
    floor_hour,
    floor_minute,
    is_active,
    is_online,
    rollup_cutoffs,
)


def test_delta_normal() -> None:
    d = compute_delta(Counter(100, 5), Counter(350, 9))
    assert (d.bytes, d.packets, d.reset) == (250, 4, False)
    assert d.state == Counter(350, 9)


def test_first_observation_is_base() -> None:
    d = compute_delta(None, Counter(10_000, 100))
    assert (d.bytes, d.packets) == (0, 0)
    assert d.state == Counter(10_000, 100)


def test_reset_never_negative() -> None:
    d = compute_delta(Counter(5000, 50), Counter(100, 2))
    assert (d.bytes, d.packets, d.reset) == (0, 0, True)
    assert d.state == Counter(100, 2)
    d = compute_delta(Counter(5000, 50), Counter(6000, 10))  # packets went backwards
    assert d.bytes == 0 and d.reset


def test_equal_counters_zero_delta() -> None:
    d = compute_delta(Counter(5, 5), Counter(5, 5))
    assert (d.bytes, d.packets, d.reset) == (0, 0, False)


def test_activity_thresholds_strict() -> None:
    assert is_active(2049, 11)
    assert not is_active(2048, 11)
    assert not is_active(2049, 10)
    assert not is_active(0, 0)
    assert is_active(100, 5, min_bytes=50, min_packets=4)


def test_online_needs_two_polls() -> None:
    assert not is_online([])
    assert not is_online([True])
    assert not is_online([True, False])
    assert not is_online([False, True])
    assert is_online([False, True, True])


def test_floors_in_utc() -> None:
    ts = datetime(2026, 3, 10, 12, 34, 56, 789, tzinfo=UTC)
    assert floor_minute(ts) == datetime(2026, 3, 10, 12, 34, tzinfo=UTC)
    assert floor_hour(ts) == datetime(2026, 3, 10, 12, tzinfo=UTC)
    assert floor_day(ts) == datetime(2026, 3, 10, tzinfo=UTC)
    plus3 = datetime(2026, 3, 10, 1, 30, tzinfo=timezone(timedelta(hours=3)))
    assert floor_day(plus3) == datetime(2026, 3, 9, tzinfo=UTC)


def test_rollup_cutoffs() -> None:
    c = rollup_cutoffs(NOW)
    assert c.minute_before == datetime(2026, 2, 24, 12, tzinfo=UTC)
    assert c.hour_before == floor_day(NOW - timedelta(days=180))
    c2 = rollup_cutoffs(NOW, RetentionPolicy(minute_days=1, hour_days=2))
    assert c2.minute_before == datetime(2026, 3, 9, 12, tzinfo=UTC)


def test_choose_granularity() -> None:
    assert choose_granularity(NOW - timedelta(hours=24), NOW, NOW) == "minute"
    assert choose_granularity(NOW - timedelta(days=7), NOW, NOW) == "hour"
    assert choose_granularity(NOW - timedelta(days=30), NOW, NOW) == "hour"
    assert choose_granularity(NOW - timedelta(days=400), NOW, NOW) == "day"
    assert choose_granularity(NOW - timedelta(days=100), NOW - timedelta(days=99), NOW) == "hour"
    assert choose_granularity(NOW - timedelta(days=20), NOW - timedelta(days=19), NOW) == "hour"
    assert DEFAULT_RETENTION.minute_days == 14
