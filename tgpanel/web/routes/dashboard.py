"""Dashboard and the apply-status fragment."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import UserFilter, UserListQuery
from tgpanel.web.deps import DashboardView, WebContext, safe
from tgpanel.web.routes.common import (
    banner_state,
    get_web,
    render,
    require_auth,
)

router = APIRouter(dependencies=[Depends(require_auth)])


async def _count(web: WebContext, f: UserFilter) -> int:
    return (await web.app.users.list(UserListQuery(filter=f, per_page=50))).total


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
    runs = await web.app.db.run(repo.list_apply_runs, 5)
    return await render(
        request, "dashboard.html", page="dashboard", v=view, degraded=degraded, runs=runs
    )


@router.get("/apply/status")
async def apply_status(request: Request) -> Response:
    web = get_web(request)
    return await render(request, "_banner.html", banner=await banner_state(web))
