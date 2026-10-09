"""Read-only traffic and dashboard queries for web/bot (PLAN 5.2).

Never writes to the database and never touches the system except the relay's ``/metrics``
(cached) and ``/healthz``. ``/readyz`` is NEVER called here.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from tgpanel.apply.settings_spec import SPECS
from tgpanel.collector import _config as cfg
from tgpanel.collector.rollup import retention_policy
from tgpanel.db import repo, times
from tgpanel.db.connection import Database
from tgpanel.domain import pools as pools_domain
from tgpanel.domain.counters import (
    choose_granularity,
    floor_day,
    floor_hour,
    floor_minute,
)
from tgpanel.domain.models import UserStatus
from tgpanel.domain.queries import Period
from tgpanel.system.ops import SystemOps, SystemOpsError

RELAY_METRICS_URL = "http://127.0.0.1:8081/metrics"
RELAY_HEALTHZ_URL = "http://127.0.0.1:8081/healthz"
RELAY_TIMEOUT_S = 2.0
GAUGES_TTL_S = 10.0
MAX_FILLED_POINTS = 20_000

_STEP = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}
_FLOOR: dict[str, Callable[[datetime], datetime]] = {
    "minute": floor_minute,
    "hour": floor_hour,
    "day": floor_day,
}
_PERIOD: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    ts: datetime
    up: int
    down: int


@dataclass(frozen=True, slots=True)
class SeriesResult:
    granularity: str  # "minute" | "hour" | "day"
    points: tuple[SeriesPoint, ...]
    total_up: int
    total_down: int

    @property
    def total(self) -> int:
        return self.total_up + self.total_down


@dataclass(frozen=True, slots=True)
class Totals:
    up: int
    down: int

    @property
    def total(self) -> int:
        return self.up + self.down


@dataclass(frozen=True, slots=True)
class PoolUsage:
    pool_id: int
    port: int
    used: int
    capacity: int

    @property
    def free(self) -> int:
        return max(self.capacity - self.used, 0)


@dataclass(frozen=True, slots=True)
class DashboardStats:
    users_total: int
    users_active: int
    users_disabled: int
    users_expired: int
    online: int
    traffic_24h: Totals
    traffic_30d: Totals
    pools: tuple[PoolUsage, ...]

    @property
    def slots_used(self) -> int:
        return sum(p.used for p in self.pools)

    @property
    def slots_free(self) -> int:
        return sum(p.free for p in self.pools)


@dataclass(frozen=True, slots=True)
class RelayGauges:
    """Global relay gauges from /metrics. Missing values are None; available=False on error."""

    available: bool
    sessions_live: float | None = None
    streams_live: float | None = None
    limit_hits_total: float | None = None
    bytes_up_total: float | None = None
    bytes_down_total: float | None = None
    sessions_created_total: float | None = None


_GAUGE_FIELDS = {
    "tproxy_sessions_live": "sessions_live",
    "tproxy_streams_live": "streams_live",
    "tproxy_limit_hits_total": "limit_hits_total",
    "tproxy_bytes_up_total": "bytes_up_total",
    "tproxy_bytes_down_total": "bytes_down_total",
    "tproxy_sessions_created_total": "sessions_created_total",
}
_METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")


def parse_metrics(text: str) -> RelayGauges:
    """Parse Prometheus text; samples of one metric with different labels are summed."""
    sums: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if m is None or m.group(1) not in _GAUGE_FIELDS:
            continue
        try:
            value = float(m.group(2))
        except ValueError:
            continue
        sums[m.group(1)] = sums.get(m.group(1), 0.0) + value
    return RelayGauges(True, **{_GAUGE_FIELDS[k]: v for k, v in sums.items()})


class _GaugeCache:
    def __init__(self) -> None:
        self.value: RelayGauges | None = None
        self.stamp = 0.0
        self.lock = asyncio.Lock()


_caches: weakref.WeakKeyDictionary[Any, _GaugeCache] = weakref.WeakKeyDictionary()


async def fetch_relay_gauges(
    ops: SystemOps, *, monotonic: Callable[[], float] = time.monotonic
) -> RelayGauges:
    """Read ``/metrics`` at most once per 10 s per ``ops`` (failures are cached as well)."""
    cache = _caches.setdefault(ops, _GaugeCache())
    async with cache.lock:
        now = monotonic()
        if cache.value is not None and 0 <= now - cache.stamp < GAUGES_TTL_S:
            return cache.value
        try:
            res = await ops.http_get(RELAY_METRICS_URL, RELAY_TIMEOUT_S)
            value = (
                parse_metrics(res.body.decode("utf-8", "replace"))
                if res.status == 200
                else RelayGauges(False)
            )
        except SystemOpsError:
            value = RelayGauges(False)
        cache.value, cache.stamp = value, now
        return value


async def relay_healthy(ops: SystemOps) -> bool:
    """``/healthz`` of the relay (cheap). NEVER ``/readyz``: it dials every profile backend."""
    try:
        res = await ops.http_get(RELAY_HEALTHZ_URL, RELAY_TIMEOUT_S)
    except SystemOpsError:
        return False
    return res.status == 200


# --------------------------------------------------------------------------- service


def _sum_tiers(
    conn: sqlite3.Connection, user_id: int | None, since: datetime | None
) -> tuple[int, int]:
    """(up, down) over the three tiers; each tier from the bucket that contains ``since``."""
    up = down = 0
    for tier, table in repo.TRAFFIC_TIERS.items():
        sql = f"SELECT COALESCE(SUM(bytes_up), 0), COALESCE(SUM(bytes_down), 0) FROM {table}"  # noqa: S608
        where: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            where.append("bucket_ts >= ?")
            params.append(times.to_epoch(_FLOOR[tier](since)))
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = conn.execute(sql, params).fetchone()
        up += int(row[0])
        down += int(row[1])
    return up, down


class TrafficService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = _utc_now) -> None:
        self._db = db
        self._clock = clock

    # ---- series

    async def user_series(
        self,
        user_id: int,
        start: datetime,
        end: datetime,
        max_points: int = 1500,
        *,
        fill_gaps: bool = True,
    ) -> SeriesResult:
        """Up/down series of one user over [start, end) at an automatically chosen granularity.

        Tiers hold disjoint data (rollup moves it), so reading all three and re-bucketing to
        the chosen granularity never double counts. Coarser data cannot be split: when the
        window reaches into a coarser tier the point sits at that tier's bucket start.
        """
        now = self._clock()
        settings, rows = await self._db.run(self._read_series, user_id, start, end, now)
        policy = retention_policy(settings)
        gran = choose_granularity(start, end, now, policy, max_points)
        floor = _FLOOR[gran]
        first = floor(start)
        acc: dict[datetime, list[int]] = {}
        for ts, up, down in rows:
            bucket = floor(ts)
            if bucket < first:
                continue
            cell = acc.setdefault(bucket, [0, 0])
            cell[0] += up
            cell[1] += down
        if fill_gaps:
            step = _STEP[gran]
            if (end - first) / step <= MAX_FILLED_POINTS:
                ts = first
                while ts < end:
                    acc.setdefault(ts, [0, 0])
                    ts += step
        points = tuple(SeriesPoint(ts, v[0], v[1]) for ts, v in sorted(acc.items()))
        return SeriesResult(gran, points, sum(p.up for p in points), sum(p.down for p in points))

    @staticmethod
    def _read_series(
        conn: sqlite3.Connection, user_id: int, start: datetime, end: datetime, now: datetime
    ) -> tuple[dict[str, str], list[tuple[datetime, int, int]]]:
        settings = repo.all_settings(conn)
        rows: list[tuple[datetime, int, int]] = []
        for tier in ("day", "hour", "minute"):
            lo = _FLOOR[tier](start)
            for p in repo.get_traffic(conn, tier, user_id, lo, max(end, lo)):
                rows.append((p.ts, p.bytes_up, p.bytes_down))
        return settings, rows

    # ---- totals

    def _since(self, period: Period) -> datetime | None:
        delta = _PERIOD[period]
        return None if delta is None else self._clock() - delta

    async def user_totals(self, user_id: int, period: Period = "30d") -> Totals:
        up, down = await self._db.run(_sum_tiers, user_id, self._since(period))
        return Totals(up, down)

    async def global_totals(self, period: Period = "30d") -> Totals:
        up, down = await self._db.run(_sum_tiers, None, self._since(period))
        return Totals(up, down)

    # ---- dashboard

    async def dashboard_stats(self) -> DashboardStats:
        now = self._clock()
        return await self._db.run(self._read_dashboard, now)

    @staticmethod
    def _read_dashboard(conn: sqlite3.Connection, now: datetime) -> DashboardStats:
        counts = {s: 0 for s in UserStatus}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM users GROUP BY status"):
            counts[UserStatus(r["status"])] = int(r["n"])
        cutoff = times.to_db(now - repo.ONLINE_FRESHNESS)
        online = int(
            conn.execute(
                "SELECT COUNT(*) FROM counter_state WHERE active_last = 1 AND active_prev = 1"
                " AND COALESCE(updated_at, '') >= ?",
                (cutoff,),
            ).fetchone()[0]
        )
        d24 = _sum_tiers(conn, None, now - timedelta(hours=24))
        d30 = _sum_tiers(conn, None, now - timedelta(days=30))
        return DashboardStats(
            users_total=sum(counts.values()),
            users_active=counts[UserStatus.ACTIVE],
            users_disabled=counts[UserStatus.DISABLED],
            users_expired=counts[UserStatus.EXPIRED],
            online=online,
            traffic_24h=Totals(*d24),
            traffic_30d=Totals(*d30),
            pools=TrafficService._pools(conn),
        )

    @staticmethod
    def _pools(conn: sqlite3.Connection) -> tuple[PoolUsage, ...]:
        capacity = cfg.int_setting(
            repo.all_settings(conn),
            "secrets_per_process",
            int(SPECS["secrets_per_process"].default),
            1,
            16,
        )
        pool_list = repo.list_pools(conn)
        occ = pools_domain.occupancy(pool_list, repo.all_users(conn))
        return tuple(PoolUsage(p.id, p.port, occ[p.id], capacity) for p in pool_list)

    async def pool_usage(self) -> tuple[PoolUsage, ...]:
        """Pools with used/free slots (``secrets_per_process`` per pool)."""
        return await self._db.run(self._pools)


__all__ = [
    "DashboardStats",
    "PoolUsage",
    "RelayGauges",
    "SeriesPoint",
    "SeriesResult",
    "Totals",
    "TrafficService",
    "fetch_relay_gauges",
    "parse_metrics",
    "relay_healthy",
]
