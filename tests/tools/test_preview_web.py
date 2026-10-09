# ruff: noqa: RUF001
"""The preview tool builds a working app on fake systems with synthetic data."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from tests.tools import preview_web as pw
from tgpanel.db import repo
from tgpanel.system.fake import FakeSystemOps


async def test_preview_stack_serves_the_redesigned_panel(tmp_path: Path) -> None:
    stack = await pw.build_preview(tmp_path)
    try:
        assert isinstance(stack.pipeline.ops, FakeSystemOps)  # never a real system
        assert len(stack.ctx.db.call(repo.all_users)) == pw.USERS
        transport = httpx.ASGITransport(app=stack.app, client=("127.0.0.1", 1))
        base = f"http://{pw.HOST}:{pw.PORT}"
        async with httpx.AsyncClient(transport=transport, base_url=base) as c:
            root = f"/{pw.PANEL_PATH}"
            login = await c.get(root + "/login")
            token = re.search(r'name="login_token" value="([^"]*)"', login.text)
            assert token
            r = await c.post(
                root + "/login",
                data={"username": pw.LOGIN, "password": pw.PASSWORD, "login_token": token.group(1)},
            )
            assert r.status_code == 303
            dash = await c.get(root + "/")
            assert dash.status_code == 200 and "Сейчас онлайн" in dash.text
            assert dash.text.count('class="dot on"') == pw.ONLINE
            first = await c.get(root + "/metrics/fragment")
            assert "Мбит/с" in first.text and "eth0" in first.text
            chart = (await c.get(root + "/traffic/global.json?preset=30d")).json()
            assert chart["points"] and chart["total_down"] > chart["total_up"] > 0
            users = await c.get(root + "/users", headers={"HX-Request": "true"})
            assert users.status_code == 200 and "<html" not in users.text
            for page in ("/users", "/requests", "/backups", "/audit", "/settings"):
                assert (await c.get(root + page)).status_code == 200, page
    finally:
        stack.ctx.close()


def test_proc_simulation_changes_over_time() -> None:
    t = [0.0]
    sim = pw.ProcSim(now=lambda: t[0])
    first = sim.read("/proc/net/dev")
    t[0] += 5.0
    second = sim.read("/proc/net/dev")
    assert first != second and sim.read("/proc/uptime") is not None
    assert sim.read("/proc/unknown") is None
