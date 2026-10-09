"""End-to-end rig: the REAL composed stack (``tgpanel.main.compose``) over FakeSystemOps.

Web requests go through httpx ASGITransport into the real FastAPI app, Telegram updates through
the real aiogram dispatcher on a mocked session, the collector/scheduler run via ``poll_once`` /
``tick`` (or as supervised components). A global guard checks at the end of every test that no
secret leaked into logs, audit rows, apply_runs, system calls or any HTML page before reveal.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiogram import Dispatcher
from argon2 import PasswordHasher

from tests.apply.conftest import SECRET_RE, Clock, no_sleep
from tests.bot.helpers import TOKEN, MockSession, Tg
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.bot.app import build_dispatcher, create_bot
from tgpanel.db import repo
from tgpanel.main import AppEnv, Stack, compose, prepare
from tgpanel.system.fake import FakeSystemOps
from tgpanel.web.security import LoginLimiter

ROOT = "/panel-xyz"
LOGIN = "admin"
PASSWORD = "correct horse battery staple"
ADMIN = 1
USER = 500
PROFILES = "/etc/tproxy-server/profiles.json"
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')
REVEAL_PATH = re.compile(r"/users/(\d+/reveal|reveal-many)$")
VALID_TOKEN = "123456:" + "A" * 35

FAST = ApplyConfig(
    timing=ApplyTiming(
        healthz_attempts=3,
        readyz_attempts=2,
        healthz_interval_s=0.0,
        readyz_interval_s=0.0,
        port_timeout_s=0.1,
        lock_timeout_s=1.0,
    )
)


@dataclass
class E2E:
    stack: Stack
    fake: FakeSystemOps
    clock: Clock
    client: httpx.AsyncClient
    session: MockSession
    tg: Tg
    dispatcher: Dispatcher
    db_path: Path
    pages: list[tuple[str, str]] = field(default_factory=list)  # (path, html) of every response

    # ------------------------------------------------------------------ plumbing

    def u(self, path: str) -> str:
        return ROOT + path

    @property
    def pipeline(self) -> Any:
        return self.stack.pipeline

    def db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self.stack.ctx.db.call(fn, *args, **kwargs)

    def runs(self) -> list[repo.ApplyRun]:
        return self.db(repo.list_apply_runs, 1000)  # type: ignore[no-any-return]

    def successful_runs(self) -> list[repo.ApplyRun]:
        return [r for r in self.runs() if r.status == "success"]

    def users(self) -> list[Any]:
        return self.db(repo.all_users)  # type: ignore[no-any-return]

    def profiles(self) -> list[dict[str, Any]]:
        return list(self.fake.get_json(PROFILES)["profiles"])

    def readyz_calls(self) -> int:
        return self.fake.call_count("http_get", "/readyz")

    def known_secrets(self) -> set[str]:
        out: set[str] = set()
        conn = sqlite3.connect(self.db_path)
        try:
            for row in conn.execute("SELECT secret FROM users"):
                out.add(row[0])
                out.add(row[0][2:] if row[0].startswith("dd") else row[0])
            for row in conn.execute("SELECT value FROM settings WHERE key = 'sentinel_secret'"):
                out.add(row[0])
        finally:
            conn.close()
        return {s for s in out if len(s) >= 32}

    # ------------------------------------------------------------------ web helpers

    async def login(self, password: str = PASSWORD) -> httpx.Response:
        page = await self.client.get(self.u("/login"))
        token = re.search(r'name="login_token" value="([^"]*)"', page.text)
        assert token
        return await self.client.post(
            self.u("/login"),
            data={"username": LOGIN, "password": password, "login_token": token.group(1)},
        )

    async def csrf(self) -> str:
        page = await self.client.get(self.u("/"))
        match = CSRF_RE.search(page.text)
        assert match, page.text[:300]
        return match.group(1)

    async def post(self, path: str, data: dict[str, Any] | None = None) -> httpx.Response:
        payload = dict(data or {})
        payload["csrf_token"] = await self.csrf()
        return await self.client.post(self.u(path), data=payload)

    async def create_via_web(self, name: str, **extra: str) -> httpx.Response:
        form = {"mode": "single", "name": name, "term": "default", **extra}
        return await self.post("/users/new", form)

    async def reveal(self, user_id: int) -> str:
        res = await self.post(f"/users/{user_id}/reveal")
        assert res.status_code == 200, res.text[:300]
        return res.text

    def user_by_name(self, name: str) -> Any:
        for user in self.users():
            if user.name == name:
                return user
        raise AssertionError(f"no user {name}")

    # ------------------------------------------------------------------ bot helpers

    def link_html(self, user: Any) -> str:
        import html

        return html.escape(self.stack.ctx.users.link(user), quote=False)

    def set_setting(self, key: str, value: str) -> None:
        self.db(repo.set_setting, key, value)

    # ------------------------------------------------------------------ guard

    def assert_no_leaks(self, log_text: str) -> None:
        secrets = self.known_secrets()
        conn = sqlite3.connect(self.db_path)  # own connection: the stack may be closed already
        try:
            audit = "\n".join(
                "|".join(str(c) for c in row)
                for row in conn.execute("SELECT actor, action, target, details FROM audit_log")
            )
            runs = "\n".join(
                "|".join(str(c) for c in row)
                for row in conn.execute("SELECT error, reason, backup_path FROM apply_runs")
            )
            successful = conn.execute(
                "SELECT COUNT(*) FROM apply_runs WHERE status = 'success'"
            ).fetchone()[0]
        finally:
            conn.close()
        haystacks = {
            "logs": log_text,
            "audit": audit,
            "apply_runs": runs,
            "system calls": repr(self.fake.calls),
        }
        for path, html_text in self.pages:
            if not REVEAL_PATH.search(path):
                haystacks[f"page {path}"] = html_text
        assert self.readyz_calls() <= successful, "/readyz outside an apply"
        for where, text in haystacks.items():
            assert not SECRET_RE.search(text), f"secret-looking string in {where}"
            for secret in secrets:
                assert secret not in text, f"secret in {where}"


async def build_e2e(
    tmp_path: Path,
    variant: str = "clean",
    *,
    apply_first: bool = True,
    bot_token: str = VALID_TOKEN,
    fake: FakeSystemOps | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[E2E]:
    """``fake``/``clock`` given = a restarted "process" over the same server and database."""
    if fake is None:
        fake = FakeSystemOps()
        fake.seed_upstream(variant)  # type: ignore[arg-type]
    clock = clock or Clock()
    db_path = tmp_path / "tgpanel.db"
    env = AppEnv(
        bot_token=bot_token,
        admin_ids=(ADMIN,),
        panel_domain="panel.example.com",
        panel_path="panel-xyz",
        secret_key="k" * 40,
        db_path=str(db_path),
        listen_host="127.0.0.1",
        listen_port=8090,
        env_file=str(tmp_path / "tgpanel.env"),
    )
    hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    session = MockSession()
    stack = compose(
        env,
        fake,
        config=FAST,
        clock=clock,
        sleep=no_sleep,
        session=session,
        password_hasher=hasher,
        limiter=LoginLimiter(lambda: float(clock.now.timestamp())),
        extra_hosts={"testserver"},
        scheduler_tick_s=0.01,
    )
    db = stack.ctx.db
    db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    db.call(repo.set_setting, "panel_login", LOGIN)
    db.call(repo.set_setting, "panel_password_hash", hasher.hash(PASSWORD))
    db.call(repo.set_setting, "panel_session_version", "1")
    await prepare(stack)
    if apply_first:
        out = await stack.pipeline.apply_now("install", "system")
        assert out.ok, out.error
    stack.runtime.deps.throttle_interval = 0.0  # the tests press buttons back to back
    bot = create_bot(TOKEN, session)
    dispatcher = build_dispatcher(stack.runtime.deps)
    stack.runtime.sender.bind(bot)
    tg = Tg(dispatcher, bot, session)
    holder: list[tuple[str, str]] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        holder.append((response.request.url.path, response.text))

    transport = httpx.ASGITransport(app=stack.app, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", event_hooks={"response": [record]}
    ) as client:
        rig = E2E(stack, fake, clock, client, session, tg, dispatcher, db_path, holder)
        fake.clear_calls()
        yield rig
    stack.ctx.close()  # idempotent: tests may have closed it already


@pytest.fixture(autouse=True)
def _info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


@pytest.fixture
async def e2e(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> AsyncIterator[E2E]:
    async for rig in build_e2e(tmp_path, "clean"):
        yield rig
        rig.assert_no_leaks(caplog.text)


@pytest.fixture
async def owner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> AsyncIterator[E2E]:
    """The owner's server: 15 foreign ``user_<id>`` profiles, nothing applied yet."""
    async for rig in build_e2e(tmp_path, "owner", apply_first=False):
        yield rig
        rig.assert_no_leaks(caplog.text)
