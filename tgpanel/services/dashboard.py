"""Dashboard data: traffic statistics + relay gauges + service health + certificate expiry."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tgpanel.apply.settings_spec import read_settings
from tgpanel.db.connection import Database
from tgpanel.services.traffic import TrafficService, fetch_relay_gauges, relay_healthy
from tgpanel.system.ops import SystemOps, SystemOpsError

CERT_TTL_S = 3600.0


@dataclass(frozen=True, slots=True)
class PoolView:
    pool_id: int
    port: int
    used: int
    capacity: int


@dataclass(frozen=True, slots=True)
class DashboardView:
    users_total: int = 0
    users_active: int = 0
    users_disabled: int = 0
    users_expired: int = 0
    online: int = 0
    up_24h: int = 0
    down_24h: int = 0
    up_30d: int = 0
    down_30d: int = 0
    pools: tuple[PoolView, ...] = ()
    sessions_live: float | None = None
    max_sessions_global: int | None = None
    limit_hits_total: float | None = None
    relay_ok: bool | None = None
    services: Mapping[str, bool] = field(default_factory=dict)  # unit -> active
    cert_days_left: int | None = None


class DashboardService:
    def __init__(
        self,
        traffic: TrafficService,
        ops: SystemOps,
        db: Database,
        *,
        units: tuple[str, ...] = ("tproxy-server",),
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._traffic = traffic
        self._ops = ops
        self._db = db
        self._units = units
        self._mono = monotonic
        self._now = now
        self._cert: tuple[float, str, int | None] | None = None  # (stamp, hostname, days)

    async def _cert_days(self, hostname: str) -> int | None:
        """Days until the panel certificate expires; looked up at most once per hour."""
        if not hostname:
            return None
        cached = self._cert
        if cached is not None and cached[1] == hostname and self._mono() - cached[0] < CERT_TTL_S:
            return cached[2]
        days: int | None = None
        try:
            info = await self._ops.tls_cert_info(hostname)
        except SystemOpsError:
            info = None
        if info is not None:
            try:
                until = datetime.fromisoformat(info.not_after.replace("Z", "+00:00"))
                days = (until - self._now()).days
            except ValueError:
                days = None
        self._cert = (self._mono(), hostname, days)
        return days

    async def view(self) -> DashboardView:
        stats = await self._traffic.dashboard_stats()
        cfg = await self._db.run(read_settings)
        gauges = await fetch_relay_gauges(self._ops)
        healthy = await relay_healthy(self._ops)
        services: dict[str, bool] = {}
        for unit in self._units:
            try:
                services[unit] = await self._ops.is_active(unit)
            except SystemOpsError:
                services[unit] = False
        return DashboardView(
            users_total=stats.users_total,
            users_active=stats.users_active,
            users_disabled=stats.users_disabled,
            users_expired=stats.users_expired,
            online=stats.online,
            up_24h=stats.traffic_24h.up,
            down_24h=stats.traffic_24h.down,
            up_30d=stats.traffic_30d.up,
            down_30d=stats.traffic_30d.down,
            pools=tuple(PoolView(p.pool_id, p.port, p.used, p.capacity) for p in stats.pools),
            sessions_live=gauges.sessions_live,
            max_sessions_global=cfg.max_sessions_global,
            limit_hits_total=gauges.limit_hits_total,
            relay_ok=healthy,
            services=services,
            cert_days_left=await self._cert_days(cfg.panel_hostname),
        )
