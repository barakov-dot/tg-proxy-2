"""Web-layer fixtures: real UserServiceImpl/Importer over FakeSystemOps + a temp SQLite file."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher

from tests.apply.conftest import SECRET_RE, Clock, drop_foreign, no_sleep
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.domain.queries import Period
from tgpanel.services.api import NewUser
from tgpanel.services.container import AppContext, build_context
from tgpanel.system.fake import FakeSystemOps
from tgpanel.web.app import create_app
from tgpanel.web.deps import (
    BroadcastItem,
    BroadcastPreview,
    BroadcastReport,
    DashboardView,
    DecisionResult,
    LinkSendResult,
    PoolView,
    SeriesResult,
    Totals,
    WebContext,
)
from tgpanel.web.security import LoginLimiter

ROOT = "/panel-xyz"
LOGIN = "admin"
PASSWORD = "correct horse battery staple"
SECRET_KEY = "k" * 40
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')


class FakeTraffic:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[int, datetime | None]] = []

    async def user_series(
        self, user_id: int, start: datetime | None, end: datetime, max_points: int
    ) -> SeriesResult:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((user_id, start))
        return SeriesResult("hour", [(end, 10, 20), (end, 5, 5)], 15, 25)

    async def user_totals(self, user_id: int, period: Period) -> Totals:
        return Totals(1, 2)

    async def dashboard(self) -> DashboardView:
        if self.fail:
            raise RuntimeError("boom")
        return DashboardView(
            users_total=3,
            users_active=2,
            online=1,
            up_24h=1024,
            down_24h=2048,
            pools=(PoolView(1, 2400, 3, 16),),
            sessions_live=5.0,
            max_sessions_global=1024,
            relay_ok=True,
            services={"tproxy-server": True},
            cert_days_left=40,
        )


class FakeRequests:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, Term | None]] = []

    async def approve(self, request_id: int, term: Term | None, actor: str) -> DecisionResult:
        self.calls.append(("approve", request_id, term))
        return DecisionResult(True)

    async def reject(self, request_id: int, actor: str) -> DecisionResult:
        self.calls.append(("reject", request_id, None))
        return DecisionResult(True)


class FakeBroadcast:
    def __init__(self) -> None:
        self.started: list[tuple[str, list[int]]] = []
        self.sent_links: list[list[int]] = []

    async def preview(self, template: str, user_ids: Sequence[int]) -> BroadcastPreview:
        return BroadcastPreview(len(user_ids), len(user_ids), 0, "Пример: " + template)

    async def start(self, template: str, user_ids: Sequence[int], actor: str) -> int:
        self.started.append((template, list(user_ids)))
        return 7

    async def report(self, broadcast_id: int) -> BroadcastReport | None:
        if broadcast_id != 7:
            return None
        return BroadcastReport(7, "done", 2, 2, 0, 0, (BroadcastItem(1, 11, "sent"),))

    async def send_links(self, user_ids: Sequence[int], actor: str) -> list[LinkSendResult]:
        self.sent_links.append(list(user_ids))
        return [LinkSendResult(i, True) for i in user_ids]


@dataclass
class Web:
    client: httpx.AsyncClient
    ctx: AppContext
    web: WebContext
    fake: FakeSystemOps
    clock: Clock
    traffic: FakeTraffic
    requests: FakeRequests
    broadcast: FakeBroadcast
    logins: list[str] = field(default_factory=list)

    def u(self, path: str) -> str:
        return ROOT + path

    async def login(self, password: str = PASSWORD, username: str = LOGIN) -> httpx.Response:
        page = await self.client.get(self.u("/login"))
        token = re.search(r'name="login_token" value="([^"]*)"', page.text)
        assert token
        return await self.client.post(
            self.u("/login"),
            data={"username": username, "password": password, "login_token": token.group(1)},
        )

    async def csrf(self) -> str:
        page = await self.client.get(self.u("/"))
        m = CSRF_RE.search(page.text)
        assert m, page.text[:300]
        return m.group(1)

    async def post(
        self, path: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> httpx.Response:
        payload = dict(data or {})
        payload["csrf_token"] = await self.csrf()
        return await self.client.post(self.u(path), data=payload, **kw)

    async def create_users(self, *names: str, **kw: Any) -> list[int]:
        res = await self.ctx.users.create([NewUser(name=n, **kw) for n in names], "system")
        assert res.ok, res.error
        return list(res.user_ids)

    def secrets(self) -> set[str]:
        out: set[str] = set()
        for user in self.ctx.db.call(repo.all_users):
            out |= {user.secret, user.mtproxy_secret}
        return out

    def assert_no_secret(self, text: str) -> None:
        assert not SECRET_RE.search(text)
        for secret in self.secrets():
            assert secret not in text


async def build(tmp_path: Path, variant: str) -> AsyncIterator[Web]:
    fake = FakeSystemOps()
    fake.seed_upstream(variant)  # type: ignore[arg-type]
    if variant == "clean":
        drop_foreign(fake)
    clock = Clock()
    config = ApplyConfig(
        timing=ApplyTiming(
            healthz_attempts=3,
            readyz_attempts=2,
            healthz_interval_s=0.0,
            readyz_interval_s=0.0,
            port_timeout_s=0.1,
            lock_timeout_s=1.0,
        )
    )
    ctx = build_context(fake, tmp_path / "web.db", config=config, clock=clock, sleep=no_sleep)
    hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    ctx.db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    ctx.db.call(repo.set_setting, "panel_login", LOGIN)
    ctx.db.call(repo.set_setting, "panel_password_hash", hasher.hash(PASSWORD))
    ctx.db.call(repo.set_setting, "panel_session_version", "1")
    out = await ctx.pipeline.apply_now("init", force_external=True)
    assert out.ok, out.error
    await ctx.users.load_hostname()
    traffic, requests, broadcast = FakeTraffic(), FakeRequests(), FakeBroadcast()
    web = WebContext(
        app=ctx,
        traffic=traffic,
        requests=requests,
        broadcast=broadcast,
        secret_key=SECRET_KEY,
        password_hasher=hasher,
        limiter=LoginLimiter(lambda: float(clock.now.timestamp())),
        trusted_proxies=frozenset({"127.0.0.1"}),
    )
    app = create_app(web, ROOT)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield Web(client, ctx, web, fake, clock, traffic, requests, broadcast)
    ctx.close()


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[Web]:
    async for item in build(tmp_path, "clean"):
        yield item


@pytest.fixture
async def aw(w: Web) -> Web:
    """Logged-in client."""
    resp = await w.login()
    assert resp.status_code == 303
    return w


@pytest.fixture
async def owner_web(tmp_path: Path) -> AsyncIterator[Web]:
    async for item in build(tmp_path, "owner"):
        item2 = item
        resp = await item2.login()
        assert resp.status_code == 303
        yield item2


@pytest.fixture(autouse=True)
def _log_capture(caplog: pytest.LogCaptureFixture) -> Callable[[], str]:
    caplog.set_level(logging.DEBUG)
    return lambda: caplog.text
