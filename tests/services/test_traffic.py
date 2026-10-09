from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.collector.rollup import rollup_and_prune
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.domain.counters import Counter
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.services.traffic import (
    TrafficService,
    fetch_relay_gauges,
    parse_metrics,
    relay_healthy,
)
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import HttpResult

NOW = datetime(2026, 6, 20, 12, 30, 0, tzinfo=UTC)
METRICS = "http://127.0.0.1:8081/metrics"


@dataclass
class Env:
    db: Database
    pipeline: ApplyPipeline
    fake: FakeSystemOps
    svc: TrafficService
    uid: int

    def add(self, tier: str, ts: datetime, up: int, down: int, uid: int | None = None) -> None:
        self.db.call(repo.add_traffic, tier, uid or self.uid, ts, bytes_up=up, bytes_down=down)


def _user(db: Database, n: int, status: UserStatus = UserStatus.ACTIVE, pool: int = 1) -> int:
    return db.call(
        repo.insert_user,
        name=f"u{n}",
        secret=f"{n:032x}",
        status=status,
        pool_id=pool,
        loopback_ip=f"127.64.0.{n}",
        created_at=NOW - timedelta(days=400),
    )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    fake = FakeSystemOps()
    db = Database(tmp_path / "t.db")
    db.call(repo.insert_pool, PoolRecord(id=1, port=2400, stats_port=8900), NOW)
    db.call(repo.insert_pool, PoolRecord(id=2, port=2401, stats_port=8901), NOW)

    async def no_sleep(_: float) -> None:
        return None

    pipeline = ApplyPipeline(fake, db, ApplyConfig(timing=ApplyTiming()), sleep=no_sleep)
    uid = _user(db, 1)
    yield Env(db, pipeline, fake, TrafficService(db, lambda: NOW), uid)
    for call in fake.calls:
        assert "readyz" not in repr(call), call
    pipeline.close()
    db.close()


async def test_granularity_by_window(env: Env) -> None:
    day = timedelta(days=1)
    assert (await env.svc.user_series(env.uid, NOW - day, NOW)).granularity == "minute"
    assert (await env.svc.user_series(env.uid, NOW - 7 * day, NOW)).granularity == "hour"
    assert (await env.svc.user_series(env.uid, NOW - 30 * day, NOW)).granularity == "hour"
    assert (await env.svc.user_series(env.uid, NOW - 300 * day, NOW)).granularity == "day"


async def test_minute_series_fills_gaps_and_totals(env: Env) -> None:
    env.add("minute", NOW.replace(minute=10, second=0), 100, 40)
    env.add("minute", NOW.replace(minute=12, second=0), 5, 1)
    res = await env.svc.user_series(env.uid, NOW - timedelta(hours=1), NOW)
    assert res.granularity == "minute"
    assert len(res.points) == 60
    assert (res.total_up, res.total_down, res.total) == (105, 41, 146)
    assert sum(1 for p in res.points if p.up or p.down) == 2
    assert all(a.ts < b.ts for a, b in zip(res.points, res.points[1:], strict=False))


async def test_series_without_gap_filling(env: Env) -> None:
    env.add("minute", NOW.replace(minute=10, second=0), 1, 2)
    res = await env.svc.user_series(env.uid, NOW - timedelta(hours=1), NOW, fill_gaps=False)
    assert len(res.points) == 1


async def test_series_merges_tiers_without_double_counting(env: Env) -> None:
    # 20-day window -> hour granularity; a given hour lives in exactly one tier
    h = (NOW - timedelta(days=2)).replace(minute=0, second=0)
    for m in (0, 15, 45):
        env.add("minute", h + timedelta(minutes=m), 10, 1)
    env.add("hour", h - timedelta(hours=1), 1000, 100)
    env.add("hour", h - timedelta(days=17), 7, 7)
    res = await env.svc.user_series(env.uid, NOW - timedelta(days=20), NOW, fill_gaps=False)
    assert res.granularity == "hour"
    by_ts = {p.ts: p for p in res.points}
    assert by_ts[h].up == 30 and by_ts[h].down == 3  # three minutes folded into one hour
    assert by_ts[h - timedelta(hours=1)].up == 1000
    assert res.total_up == 30 + 1000 + 7


async def test_series_same_after_rollup(env: Env) -> None:
    base = (NOW - timedelta(days=20)).replace(minute=0, second=0)
    for i in range(0, 60 * 24 * 5, 17):
        env.add("minute", base + timedelta(minutes=i), i % 50 + 1, 2)
    start, end = NOW - timedelta(days=25), NOW
    before = await env.svc.user_series(env.uid, start, end, fill_gaps=False)
    await rollup_and_prune(env.pipeline, NOW)
    after = await env.svc.user_series(env.uid, start, end, fill_gaps=False)
    assert before.granularity == after.granularity == "hour"
    assert before.points == after.points
    assert (before.total_up, before.total_down) == (after.total_up, after.total_down)


async def test_boundary_bucket_rules(env: Env) -> None:
    start = NOW.replace(minute=0, second=0) - timedelta(days=3)
    env.add("minute", start - timedelta(minutes=1), 99, 99)  # before the window
    env.add("minute", start, 1, 1)  # first bucket is included
    end = start + timedelta(hours=2)
    env.add("minute", end, 50, 50)  # end is exclusive
    res = await env.svc.user_series(env.uid, start, end, fill_gaps=False)
    assert res.total_up == 1


async def test_start_inside_hour_bucket_keeps_it(env: Env) -> None:
    env.add("hour", (NOW - timedelta(days=20)).replace(minute=0, second=0), 500, 5)
    start = (NOW - timedelta(days=20)).replace(minute=30, second=0)
    res = await env.svc.user_series(env.uid, start, NOW, fill_gaps=False)
    assert res.total_up == 500


async def test_user_and_global_totals(env: Env) -> None:
    other = _user(env.db, 2)
    env.add("minute", NOW - timedelta(hours=2), 10, 1)
    env.add("hour", NOW - timedelta(days=3), 20, 2)
    env.add("day", NOW - timedelta(days=200), 40, 4)
    env.add("minute", NOW - timedelta(hours=1), 1000, 100, uid=other)
    assert (await env.svc.user_totals(env.uid, "24h")).up == 10
    t7 = await env.svc.user_totals(env.uid, "7d")
    assert (t7.up, t7.down, t7.total) == (30, 3, 33)
    assert (await env.svc.user_totals(env.uid, "all")).up == 70
    assert (await env.svc.global_totals("24h")).up == 1010
    assert (await env.svc.global_totals("all")).up == 1070


async def test_dashboard_stats(env: Env) -> None:
    _user(env.db, 2, UserStatus.DISABLED)
    _user(env.db, 3, UserStatus.EXPIRED, pool=2)
    u4 = _user(env.db, 4, pool=2)
    for uid, last, prev, upd in (
        (env.uid, True, True, NOW - timedelta(seconds=30)),  # online
        (u4, True, True, NOW - timedelta(minutes=10)),  # stale collector
    ):
        env.db.call(
            repo.put_counter_state,
            repo.CounterStateRow(uid, Counter(0, 0), Counter(0, 0), last, prev, upd),
        )
    env.add("minute", NOW - timedelta(hours=1), 10, 1)
    env.add("hour", NOW - timedelta(days=20), 100, 10)
    stats = await env.svc.dashboard_stats()
    assert (stats.users_total, stats.users_active) == (4, 2)
    assert (stats.users_disabled, stats.users_expired, stats.online) == (1, 1, 1)
    assert (stats.traffic_24h.up, stats.traffic_24h.down) == (10, 1)
    assert (stats.traffic_30d.up, stats.traffic_30d.down) == (110, 11)
    p1, p2 = stats.pools
    assert (p1.used, p1.capacity, p1.free) == (2, 16, 14)
    assert (p2.used, p2.free) == (2, 14)
    assert (stats.slots_used, stats.slots_free) == (4, 28)


async def test_pool_capacity_follows_secrets_per_process(env: Env) -> None:
    env.db.call(repo.set_setting, "secrets_per_process", "15")
    (p1, _p2) = await env.svc.pool_usage()
    assert (p1.capacity, p1.used, p1.free) == (15, 1, 14)


async def test_empty_database_dashboard(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    stats = await TrafficService(db, lambda: NOW).dashboard_stats()
    assert stats.users_total == 0 and stats.pools == () and stats.online == 0
    db.close()


# ------------------------------------------------------------------ relay


SAMPLE = """# HELP tproxy_sessions_live x
# TYPE tproxy_sessions_live gauge
tproxy_sessions_live 12
tproxy_streams_live{kind="a"} 3
tproxy_streams_live{kind="b"} 4
tproxy_limit_hits_total 5
tproxy_bytes_up_total 1.5e3
tproxy_bytes_down_total 2000
tproxy_sessions_created_total 77
other_metric 1
broken line
"""


def test_parse_metrics() -> None:
    g = parse_metrics(SAMPLE)
    assert g.available
    assert (g.sessions_live, g.streams_live, g.limit_hits_total) == (12, 7, 5)
    assert (g.bytes_up_total, g.bytes_down_total, g.sessions_created_total) == (1500, 2000, 77)
    empty = parse_metrics("")
    assert empty.available and empty.sessions_live is None


async def test_gauges_cached_for_ten_seconds() -> None:
    fake = FakeSystemOps()
    fake.set_http(METRICS, 200, SAMPLE.encode())
    t = [100.0]
    first = await fetch_relay_gauges(fake, monotonic=lambda: t[0])
    t[0] += 9.9
    second = await fetch_relay_gauges(fake, monotonic=lambda: t[0])
    assert second is first and fake.call_count("http_get", "/metrics") == 1
    t[0] += 0.2
    await fetch_relay_gauges(fake, monotonic=lambda: t[0])
    assert fake.call_count("http_get", "/metrics") == 2
    assert all(c[1] == METRICS for c in fake.calls_of("http_get"))


async def test_gauges_unavailable_is_cached_too() -> None:
    fake = FakeSystemOps()
    fake.set_http(METRICS, 0)
    t = [1.0]
    assert not (await fetch_relay_gauges(fake, monotonic=lambda: t[0])).available
    assert not (await fetch_relay_gauges(fake, monotonic=lambda: t[0])).available
    assert fake.call_count("http_get") == 1


async def test_gauges_ops_error_is_handled() -> None:
    fake = FakeSystemOps()
    fake.fail_on("http_get", times=1)
    assert not (await fetch_relay_gauges(fake)).available


async def test_relay_healthy_uses_healthz_only() -> None:
    fake = FakeSystemOps()
    assert await relay_healthy(fake)
    fake.set_http("/healthz", 503)
    assert not await relay_healthy(fake)
    fake.fail_on("http_get", times=1)
    fake.set_http("/healthz", 200)
    assert not await relay_healthy(fake)
    assert {c[1] for c in fake.calls_of("http_get")} == {"http://127.0.0.1:8081/healthz"}
    assert HttpResult(200).status == 200


async def test_global_series_merges_users_and_tiers(env: Env) -> None:
    second = _user(env.db, 2)
    h = (NOW - timedelta(days=2)).replace(minute=0, second=0)
    for m in (0, 30):  # minute tier, two users in the same hour -> one bucket
        env.add("minute", h + timedelta(minutes=m), 10, 1)
        env.add("minute", h + timedelta(minutes=m), 5, 2, uid=second)
    env.add("hour", h - timedelta(hours=1), 1000, 100, uid=second)  # hour tier
    env.add("day", (NOW - timedelta(days=9)).replace(hour=0, minute=0, second=0), 7, 7)  # day tier
    res = await env.svc.global_series(NOW - timedelta(days=20), NOW, fill_gaps=False)
    assert res.granularity == "hour"
    by_ts = {p.ts: p for p in res.points}
    assert (by_ts[h].up, by_ts[h].down) == (30, 6)
    assert by_ts[h - timedelta(hours=1)].up == 1000
    assert res.total_up == 30 + 1000 + 7 and res.total_down == 6 + 100 + 7
    # a per-user series is unaffected by the other user
    own = await env.svc.user_series(env.uid, NOW - timedelta(days=20), NOW, fill_gaps=False)
    assert own.total_up == 20 + 7


async def test_global_series_granularity_and_empty_window(env: Env) -> None:
    day = timedelta(days=1)
    assert (await env.svc.global_series(NOW - day, NOW)).granularity == "minute"
    assert (await env.svc.global_series(NOW - 30 * day, NOW)).granularity == "hour"
    assert (await env.svc.global_series(NOW - 300 * day, NOW)).granularity == "day"
    res = await env.svc.global_series(NOW - timedelta(hours=1), NOW, fill_gaps=False)
    assert res.points == () and (res.total_up, res.total_down) == (0, 0)
