"""Operations run strictly one at a time; operations queued meanwhile share ONE next apply."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

from tests.apply.conftest import PROFILES, RELAY, Env, add_users, make_env
from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.system.fake import FakeSystemOps


class GatedFake(FakeSystemOps):
    """Blocks the first relay restart until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.reached = asyncio.Event()
        self.block_next = True
        self.active_now = 0
        self.max_parallel = 0

    async def systemctl(self, action: str, unit: str) -> None:
        if (action, unit) == ("restart", RELAY) and self.block_next:
            self.block_next = False
            self.reached.set()
            await self.gate.wait()
        await super().systemctl(action, unit)

    async def acquire_lock(self, path: str, timeout_s: float) -> Any:
        handle = await super().acquire_lock(path, timeout_s)
        self.active_now += 1
        self.max_parallel = max(self.max_parallel, self.active_now)
        inner = handle.release

        async def release() -> None:
            self.active_now -= 1
            await inner()

        handle.release = release  # type: ignore[method-assign]
        return handle


async def _env(make: Callable[..., Env]) -> tuple[Env, GatedFake]:
    fake = GatedFake()
    fake.block_next = False
    env = make("clean", fake=fake)
    assert (await env.pipeline.apply_now("init", force_external=True)).ok
    fake.block_next = True
    fake.clear_calls()
    return env, fake


async def test_queued_operations_share_one_next_apply(make: Callable[..., Env]) -> None:
    env, fake = await _env(make)
    runs_before = len(env.runs())
    relay_before = fake.restart_count(RELAY)
    executed: list[str] = []

    def named(tag: str, n: int) -> Callable[[sqlite3.Connection], list[int]]:
        inner = add_users(env.clock, n)

        def mutation(conn: sqlite3.Connection) -> list[int]:
            executed.append(tag)
            return inner(conn)

        return mutation

    first = asyncio.create_task(env.pipeline.run_operation(named("A", 1), reason="A"))
    await fake.reached.wait()
    assert env.pipeline.is_applying
    second = asyncio.create_task(env.pipeline.run_operation(named("B", 2), reason="B"))
    third = asyncio.create_task(env.pipeline.run_operation(named("C", 1), reason="C"))

    def refuse(conn: sqlite3.Connection) -> None:
        executed.append("D")
        raise OperationRejected("отказ")

    fourth = asyncio.create_task(env.pipeline.run_operation(refuse, reason="D"))
    await asyncio.sleep(0)
    assert executed == ["A"]  # queued mutations have NOT run while the first apply is running
    fake.gate.set()
    a, b, c, d = await asyncio.gather(first, second, third, fourth)

    assert executed == ["A", "B", "C", "D"]
    assert a.ok and b.ok and c.ok
    assert not d.ok and d.status == "rejected" and d.error == "отказ"
    assert a.apply_run_id != b.apply_run_id
    assert b.apply_run_id == c.apply_run_id  # one shared apply
    assert len(b.value or []) == 2 and len(c.value or []) == 1  # each gets its own result
    assert len(env.runs()) == runs_before + 2
    assert fake.restart_count(RELAY) == relay_before + 2
    assert fake.max_parallel == 1
    assert len(env.users()) == 4
    names = [p["name"] for p in fake.get_json(PROFILES)["profiles"]]
    assert names == ["u1", "u2", "u3", "u4"]
    assert not env.pipeline.is_applying


async def test_queued_batch_failure_fails_every_member_and_rolls_back_all(
    make: Callable[..., Env],
) -> None:
    env, fake = await _env(make)
    first = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()
    second = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="B"))
    third = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="C"))
    await asyncio.sleep(0)
    fake.fail_on("systemctl", f"restart {RELAY}", skip=1)  # fails the combined second apply
    fake.gate.set()
    a, b, c = await asyncio.gather(first, second, third)
    assert a.ok
    assert not b.ok and not c.ok
    assert b.apply_run_id == c.apply_run_id
    assert len(env.users()) == 1  # only A survived; B and C left no trace in the DB
    assert [p["name"] for p in fake.get_json(PROFILES)["profiles"]] == ["u1"]


async def test_cancelled_waiter_does_not_break_the_queue(make: Callable[..., Env]) -> None:
    env, fake = await _env(make)
    first = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()
    doomed = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="B"))
    keeper = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="C"))
    await asyncio.sleep(0)
    doomed.cancel()
    fake.gate.set()
    a, c = await asyncio.gather(first, keeper)
    assert a.ok and c.ok
    assert len(env.users()) == 2  # the cancelled operation never ran
    try:
        await doomed
    except asyncio.CancelledError:
        pass


async def test_sequential_operations_do_not_share_an_apply(env: Env) -> None:
    before = len(env.runs())
    for _ in range(3):
        assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    assert len(env.runs()) == before + 3


async def test_cross_process_lock_is_taken_for_every_batch(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert env.fake.calls_of("acquire_lock", "/run/tgpanel/apply.lock")
    assert not env.fake.is_locked("/run/tgpanel/apply.lock")


async def test_lock_timeout_fails_cleanly(make: Callable[..., Env]) -> None:
    env = make_env_with_busy_lock(make)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok and "блокировк" in (out.error or "")
    assert env.users() == []


def make_env_with_busy_lock(make: Callable[..., Env]) -> Env:
    env = make("clean")
    env.fake.fail_on("acquire_lock", times=1)
    return env


async def test_db_write_waits_for_open_transaction(make: Callable[..., Env]) -> None:
    env, fake = await _env(make)
    first = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()
    order: list[str] = []

    def write(conn: sqlite3.Connection) -> None:
        order.append("write")
        repo.set_setting(conn, "timezone", "UTC")

    writer = asyncio.create_task(env.pipeline.db_write(write))
    await asyncio.sleep(0.01)
    assert order == []  # blocked while the operation transaction is open
    fake.gate.set()
    await first
    await writer
    assert order == ["write"]


_ = make_env
