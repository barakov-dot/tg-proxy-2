"""Dashboard, its live fragments (server metrics, apply banner) and the global traffic chart."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from tgpanel.db import repo
from tgpanel.domain.models import UserStatus, shown_name
from tgpanel.services.api import UserFilter, UserListQuery
from tgpanel.services.dashboard import DashboardView
from tgpanel.web.deps import WebContext, safe
from tgpanel.web.routes.common import (
    HttpError,
    banner_state,
    get_web,
    render,
    require_auth,
    series_response,
)
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])

ONLINE_LIMIT = 20
GLOBAL_PRESETS = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


async def _count(web: WebContext, f: UserFilter) -> int:
    return (await web.app.users.list(UserListQuery(filter=f, per_page=50))).total


async def _online(web: WebContext) -> tuple[list[dict[str, object]], int]:
    """Users online now (most recently active first) with the data the panel shows."""
    query = UserListQuery(
        filter=UserFilter(online=True),
        sort="last_seen_at",
        descending=True,
        period="24h",
        per_page=50,
    )
    page = await web.app.users.list(query)
    rows = page.rows[:ONLINE_LIMIT]

    def extras(conn: object) -> dict[int, repo.UserExtra | None]:
        return {r.user.id: repo.get_user_extra(conn, r.user.id) for r in rows}  # type: ignore[arg-type]

    extra = await web.app.db.run(extras)
    out: list[dict[str, object]] = []
    for r in rows:
        ex = extra.get(r.user.id)
        out.append(
            {
                "id": r.user.id,
                "name": shown_name(r.user),
                "tg_id": r.user.tg_id,
                "tg_username": ex.tg_username if ex else None,
                "pool_id": r.user.pool_id,
                "last_seen": r.last_seen_at,
                "up": r.bytes_up,
                "down": r.bytes_down,
            }
        )
    return out, page.total


@router.get("/")
async def dashboard(request: Request) -> Response:
    web = get_web(request)
    view = await safe(web.traffic.dashboard(), "dashboard")
    degraded = view is None
    if view is None:
        # fall back to counts from the user service so the page still says something useful
        view = DashboardView(
            users_total=await _count(web, UserFilter()),
            users_active=await _count(web, UserFilter(statuses=(UserStatus.ACTIVE,))),
            online=await _count(web, UserFilter(online=True)),
        )
    online, online_total = await _online(web)
    return await render(
        request,
        "dashboard.html",
        page="dashboard",
        v=view,
        degraded=degraded,
        online=online,
        online_total=online_total,
        online_limit=ONLINE_LIMIT,
    )


@router.get("/apply/status")
async def apply_status(request: Request) -> Response:
    web = get_web(request)
    return await render(request, "_banner.html", banner=await banner_state(web))


@router.get("/metrics/fragment")
async def metrics_fragment(request: Request) -> Response:
    web = get_web(request)
    view = await safe(web.metrics.snapshot(), "metrics") if web.metrics is not None else None
    return await render(request, "_metrics.html", m=view)


@router.get("/traffic/global.json")
async def global_traffic(request: Request) -> Response:
    web = get_web(request)
    preset = request.query_params.get("preset", "1d")
    if preset not in GLOBAL_PRESETS:
        raise HttpError(400, T["bad_filter"])
    end: datetime = web.now()
    series = await safe(
        web.traffic.global_series(end - GLOBAL_PRESETS[preset], end, 600), "global_series"
    )
    if series is None:
        return JSONResponse({"error": T["traffic_unavailable"]}, status_code=503)
    return series_response(series)
