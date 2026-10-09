"""(g) serialised and coalesced operations, (h) collector vs apply writes, (i) component crash
isolation, (j) graceful shutdown during an apply, (k) crash recovery, (m) /readyz discipline."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import E2E, USER, build_e2e
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.main import ScrubFilter, Supervisor, build_components, run_stack
from tgpanel.services.api import NewUser

JOURNAL = "/var/lib/tgpanel/apply.journal"


class RestartProbe:
    """Wraps ``systemctl restart tproxy-server``: counts overlap and can hold the restart."""

    def __init__(self, e2e: E2E, delay: float = 0.0) -> None:
        self.inflight = 0
        self.max_inflight = 0
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self._delay = delay
        self._orig = e2e.fake.systemctl
        e2e.fake.systemctl = self._wrapped  # type: ignore[method-assign]

    async def _wrapped(self, action: str, unit: str) -> None:
        if action == "restart" and unit == "tproxy-server":
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.entered.set()
            try:
                if self.gate is not None:
                    await self.gate.wait()
                await asyncio.sleep(self._delay)
                await self._orig(action, unit)
            finally:
                self.inflight -= 1
        else:
            await self._orig(action, unit)


async def test_concurrent_web_and_bot_operations_are_serialised_and_coalesced(e2e: E2E) -> None:
    assert (await e2e.login()).status_code == 303
    e2e.set_setting("issuance_mode", "open")
    probe = RestartProbe(e2e, delay=0.02)
    runs_before = len(e2e.successful_runs())
    results = await asyncio.gather(
        *[e2e.create_via_web(f"web-{i}") for i in range(4)],
        e2e.tg.press(USER, "req"),
        e2e.stack.ctx.users.create([NewUser("svc-1"), NewUser("svc-2")], "system"),
    )
    assert all(isinstance(r, httpx.Response) and r.status_code == 200 for r in results[:4])
    assert probe.max_inflight == 1  # never two relay restarts at once
    names = {u.name for u in e2e.users()}
    assert {f"web-{i}" for i in range(4)} <= names and {"svc-1", "svc-2"} <= names
    assert e2e.stack.ctx.db.call(repo.get_user_by_tg_id, USER) is not None
    applies = len(e2e.successful_runs()) - runs_before
    assert 1 <= applies < 6, applies  # 6 operations, coalesced into fewer applies
    assert e2e.readyz_calls() == applies  # exactly one /readyz per apply, none otherwise
    pool_names = {p["name"] for p in e2e.profiles()}
    assert all(f"u{u.id}" in pool_names for u in e2e.users())


async def test_collector_and_apply_write_concurrently_without_lock_errors(
    e2e: E2E, caplog: pytest.LogCaptureFixture
) -> None:
    assert (await e2e.login()).status_code == 303
    first = await e2e.stack.ctx.users.create(
        [NewUser("m1"), NewUser("m2"), NewUser("toggle")], "system"
    )
    assert first.ok
    ips = [u.loopback_ip for u in e2e.users() if u.name in ("m1", "m2")]
    await e2e.stack.collector.poll_once(e2e.clock())  # baseline
    RestartProbe(e2e, delay=0.03)
    expected = 0
    stop = asyncio.Event()

    async def collect() -> None:
        nonlocal expected
        while not stop.is_set():
            for ip in ips:
                e2e.fake.add_traffic(ip, up_bytes=1000, down_bytes=3000, up_packets=20)
                expected += 4000
            e2e.clock.now += timedelta(seconds=30)
            await e2e.stack.collector.poll_once(e2e.clock())
            await asyncio.sleep(0.005)

    async def writes() -> None:
        for i in range(4):
            res = await e2e.create_via_web(f"during-{i}")
            assert res.status_code == 200
            await e2e.stack.ctx.users.set_status([first.user_ids[2]], bool(i % 2), "system")
        stop.set()

    await asyncio.wait_for(asyncio.gather(collect(), writes()), 30)
    e2e.clock.now += timedelta(seconds=30)
    await e2e.stack.collector.poll_once(e2e.clock())
    assert "locked" not in caplog.text.lower()
    assert "collector poll failed" not in caplog.text
    total = 0
    for u in e2e.users():
        t = await e2e.stack.traffic.user_totals(u.id, "all")
        total += t.up + t.down
    assert expected > 0 and total == expected  # every byte counted exactly once


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_bot_crash_does_not_stop_web_collector_scheduler(
    e2e: E2E, caplog: pytest.LogCaptureFixture
) -> None:
    port = _free_port()
    stop = asyncio.Event()
    attempts = 0

    async def broken_bot(_: asyncio.Event) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"cannot start {e2e.stack.env.bot_token} {e2e.stack.env.secret_key}")

    scrub = ScrubFilter([e2e.stack.env.secret_key, e2e.stack.env.bot_token]).scrub
    sup = Supervisor(stop, scrub=scrub, base_delay_s=0.01, max_delay_s=0.05, grace_s=10)
    components = build_components(e2e.stack, port=port)
    assert set(components) == {"web", "collector", "bot", "scheduler"}
    runner = asyncio.create_task(
        run_stack(
            e2e.stack,
            stop,
            components=components,
            overrides={"bot": broken_bot},
            supervisor=sup,
            close=False,
        )
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            body: dict[str, Any] = {}
            for _ in range(200):
                with contextlib.suppress(httpx.TransportError):
                    resp = await client.get("/panel-xyz/healthz")
                    if resp.status_code == 200:
                        body = resp.json()
                        break
                await asyncio.sleep(0.02)
            assert body == {"ok": True}
            for _ in range(200):
                if attempts >= 3:
                    break
                await asyncio.sleep(0.02)
            assert attempts >= 3 and sup.restarts["bot"] >= 2  # restarted with backoff
            # the web panel keeps serving (login page), the others never restarted
            assert (await client.get("/panel-xyz/login")).status_code == 200
            assert sup.restarts["web"] == sup.restarts["collector"] == 0
            assert sup.restarts["scheduler"] == 0
            assert e2e.fake.call_count("nft_list_set") >= 2  # the collector polled
    finally:
        stop.set()
        assert await asyncio.wait_for(runner, 30) == 0
    text = caplog.text
    assert "component bot crashed: RuntimeError" in text
    assert e2e.stack.env.bot_token not in text and e2e.stack.env.secret_key not in text


async def test_disabled_bot_is_loud_but_everything_else_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async for rig in build_e2e(tmp_path, "clean", bot_token=""):
        assert not rig.stack.bot_enabled
        components = build_components(rig.stack, serve_web=False)
        assert set(components) == {"collector", "scheduler"}
        assert "Telegram-бот отключён" in caplog.text
        stop = asyncio.Event()
        runner = asyncio.create_task(run_stack(rig.stack, stop, components=components, close=False))
        await asyncio.sleep(0.1)
        assert not runner.done()
        stop.set()
        assert await asyncio.wait_for(runner, 10) == 0


async def test_graceful_shutdown_waits_for_inflight_apply(e2e: E2E) -> None:
    assert (await e2e.login()).status_code == 303
    probe = RestartProbe(e2e)
    probe.gate = asyncio.Event()
    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_stack(e2e.stack, stop, components=build_components(e2e.stack, serve_web=False))
    )
    create = asyncio.create_task(e2e.create_via_web("Идёт применение"))
    await asyncio.wait_for(probe.entered.wait(), 10)
    assert e2e.pipeline.is_applying
    stop.set()  # SIGTERM arrives while the relay restart is held
    await asyncio.sleep(0.2)
    assert not runner.done(), "the service exited in the middle of an apply"
    probe.gate.set()
    assert await asyncio.wait_for(runner, 30) == 0
    response = await create
    assert response.status_code == 200
    conn = __import__("sqlite3").connect(e2e.db_path)
    try:
        statuses = [r[0] for r in conn.execute("SELECT status FROM apply_runs")]
        assert statuses.count("running") == 0 and statuses[-1] == "success"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        conn.close()
    assert JOURNAL not in e2e.fake.files


async def test_wait_apply_idle_is_bounded() -> None:
    from tgpanel.main import wait_apply_idle

    class Busy:
        is_applying = True

    class Idle:
        is_applying = False

    assert not await wait_apply_idle(Busy(), 0.05, poll_s=0.01)  # type: ignore[arg-type]
    assert await wait_apply_idle(Idle(), 0.05)  # type: ignore[arg-type]


class Crash(BaseException):
    """Simulates the process dying (no rollback code runs)."""


async def test_restart_after_crash_mid_apply_restores_files(
    e2e: E2E, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (await e2e.login()).status_code == 303
    await e2e.create_via_web("Живой")

    def snapshot(rig: E2E) -> dict[str, tuple[bytes, int, str, str]]:
        return {
            p: (f.data, f.mode, f.owner, f.group)
            for p, f in rig.fake.files.items()
            if not p.startswith("/var/backups/") and p != JOURNAL
        }

    before = snapshot(e2e)

    async def no_cleanup(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(ApplyPipeline, "_fail", no_cleanup)
    e2e.fake.fail_on("systemctl", "restart tproxy-server", exc=Crash)  # type: ignore[arg-type]
    doomed = await e2e.create_via_web("Погибший")
    assert doomed.status_code == 409
    with pytest.raises(Crash):
        await e2e.pipeline._driver
    monkeypatch.undo()
    assert snapshot(e2e) != before  # half-applied state on disk
    assert JOURNAL in e2e.fake.files
    e2e.pipeline.close()  # "the process died"
    e2e.stack.ctx.db.close()

    fake, clock = e2e.fake, e2e.clock
    async for restarted in build_e2e(tmp_path, "clean", apply_first=False, fake=fake, clock=clock):
        assert snapshot(restarted) == before  # recovered byte for byte
        assert JOURNAL not in fake.files
        statuses = [r.status for r in restarted.runs()]
        assert "running" not in statuses and "failed" in statuses
        assert [u.name for u in restarted.users()] == ["Живой"]
        assert (await restarted.login()).status_code == 303
        ok = await restarted.create_via_web("После сбоя")
        assert ok.status_code == 200
        assert restarted.user_by_name("После сбоя").status is UserStatus.ACTIVE
        break


async def test_readyz_only_during_applies(e2e: E2E) -> None:
    assert (await e2e.login()).status_code == 303
    quiet: list[Callable[[], Awaitable[Any]]] = [
        lambda: e2e.client.get(e2e.u("/")),
        lambda: e2e.client.get(e2e.u("/users")),
        lambda: e2e.client.get(e2e.u("/settings")),
        lambda: e2e.stack.collector.poll_once(e2e.clock()),
        lambda: e2e.stack.dashboard.view(),
        lambda: e2e.stack.runtime.scheduler.tick(),
    ]
    for call in quiet:
        await call()
    assert e2e.readyz_calls() == 0
    await e2e.create_via_web("Один")
    assert e2e.readyz_calls() == 1
    noop = await e2e.pipeline.apply_now("noop", "system")
    assert noop.status == "noop" and e2e.readyz_calls() == 1
    e2e.fake.fail_on("systemctl", "restart tproxy-server")
    failed = await e2e.create_via_web("Сбой")
    assert failed.status_code == 409
    assert e2e.readyz_calls() == 1  # a failed apply did not poll /readyz either
    for call in quiet:
        await call()
    assert e2e.readyz_calls() == 1
