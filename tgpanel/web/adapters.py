"""Ready-made adapters from the real services to the web ports (``deps.py``).

``TrafficAdapter`` wraps ``TrafficService`` (series/totals) and ``DashboardService`` (dashboard);
``RequestsAdapter`` wraps the bot-side ``RequestService``. The broadcast port has no adapter
here: it belongs to the bot/broadcast side. Nothing here touches ``SystemOps`` directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tgpanel.domain.expiry import Term
from tgpanel.domain.queries import Period
from tgpanel.services.dashboard import DashboardService, DashboardView
from tgpanel.services.requests import RequestKind, RequestService
from tgpanel.services.traffic import TrafficService
from tgpanel.web.deps import DecisionResult, SeriesResult, Totals

ALL_TIME_DAYS = 3650


class TrafficAdapter:
    def __init__(self, service: TrafficService, dashboard: DashboardService) -> None:
        self._svc = service
        self._dash = dashboard

    async def user_series(
        self, user_id: int, start: datetime | None, end: datetime, max_points: int
    ) -> SeriesResult:
        lo = start if start is not None else end - timedelta(days=ALL_TIME_DAYS)
        res = await self._svc.user_series(user_id, lo, end, max_points)
        return SeriesResult(res.granularity, res.points, res.total_up, res.total_down)

    async def user_totals(self, user_id: int, period: Period) -> Totals:
        t = await self._svc.user_totals(user_id, period)
        return Totals(t.up, t.down)

    async def dashboard(self) -> DashboardView:
        return await self._dash.view()


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
