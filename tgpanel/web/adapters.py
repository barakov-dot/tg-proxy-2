"""Ready-made adapters from the real services to the web ports (``deps.py``).

``TrafficAdapter`` wraps ``TrafficService`` + relay gauges; ``RequestsAdapter`` wraps the bot-side
``RequestService``. The broadcast port has no adapter here: it belongs to the bot/broadcast side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tgpanel.domain.expiry import Term
from tgpanel.domain.queries import Period
from tgpanel.services.requests import RequestKind, RequestService
from tgpanel.services.traffic import (
    TrafficService,
    fetch_relay_gauges,
    relay_healthy,
)
from tgpanel.system.ops import SystemOps, SystemOpsError
from tgpanel.web.deps import (
    DashboardView,
    DecisionResult,
    PoolView,
    SeriesResult,
    Totals,
)

ALL_TIME = timedelta(days=3650)


class TrafficAdapter:
    def __init__(
        self,
        service: TrafficService,
        ops: SystemOps,
        *,
        units: tuple[str, ...] = ("tproxy-server",),
        panel_hostname: str = "",
    ) -> None:
        self._svc = service
        self._ops = ops
        self._units = units
        self._panel_hostname = panel_hostname

    async def user_series(
        self, user_id: int, start: datetime | None, end: datetime, max_points: int
    ) -> SeriesResult:
        lo = start if start is not None else end - ALL_TIME
        res = await self._svc.user_series(user_id, lo, end, max_points)
        return SeriesResult(res.granularity, res.points, res.total_up, res.total_down)

    async def user_totals(self, user_id: int, period: Period) -> Totals:
        t = await self._svc.user_totals(user_id, period)
        return Totals(t.up, t.down)

    async def dashboard(self) -> DashboardView:
        stats = await self._svc.dashboard_stats()
        gauges = await fetch_relay_gauges(self._ops)
        healthy = await relay_healthy(self._ops)
        services: dict[str, bool] = {}
        for unit in self._units:
            try:
                services[unit] = await self._ops.is_active(unit)
            except SystemOpsError:
                services[unit] = False
        days_left: int | None = None
        if self._panel_hostname:
            try:
                cert = await self._ops.tls_cert_info(self._panel_hostname)
            except SystemOpsError:
                cert = None
            if cert is not None:
                try:
                    until = datetime.fromisoformat(cert.not_after.replace("Z", "+00:00"))
                    days_left = (until - datetime.now(UTC)).days
                except ValueError:
                    days_left = None
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
            limit_hits_total=gauges.limit_hits_total,
            relay_ok=healthy,
            services=services,
            cert_days_left=days_left,
        )


class RequestsAdapter:
    def __init__(self, service: RequestService) -> None:
        self._svc = service

    async def approve(self, request_id: int, term: Term | None, actor: str) -> DecisionResult:
        out = await self._svc.approve(request_id, term, actor)
        if out.kind is RequestKind.ISSUED:
            return DecisionResult(True)
        return DecisionResult(False, out.error or f"Не удалось одобрить заявку ({out.kind.value})")

    async def reject(self, request_id: int, actor: str) -> DecisionResult:
        out = await self._svc.reject(request_id, actor)
        if out.kind is RequestKind.REJECTED:
            return DecisionResult(True)
        return DecisionResult(False, out.error or f"Не удалось отклонить заявку ({out.kind.value})")
