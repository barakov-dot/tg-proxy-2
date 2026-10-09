"""S3: open-mode issuance is coalesced (batch window) and rate limited."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.services.conftest import RELAY, Svc
from tgpanel.db import repo
from tgpanel.services.requests import RequestKind, RequestService


class Window:
    """Fake clock for the batch window: ``sleep`` blocks until the test opens the gate."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.durations: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.durations.append(seconds)
        self.started.set()
        await self.gate.wait()


@pytest.fixture
async def open_rs(svc: Svc) -> tuple[RequestService, Window]:
    await svc.users.load_hostname()
    svc.ctx.db.call(repo.set_setting, "issuance_mode", "open")
    window = Window()
    return RequestService(svc.ctx.pipeline, svc.ctx.db, svc.users, sleep=window.sleep), window


async def test_requests_in_the_window_are_issued_by_one_apply(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    preparing: list[int] = []

    def submit(tg: int) -> asyncio.Task[object]:
        async def prep() -> None:
            preparing.append(tg)

        return asyncio.ensure_future(rs.submit(tg, None, f"user {tg}", on_preparing=prep))

    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    tasks = [submit(tg) for tg in (10, 11, 12, 13, 14)]
    await window.started.wait()
    await asyncio.sleep(0.05)
    # "готовим…" went out immediately, nothing has been created yet
    assert sorted(preparing) == [10, 11, 12, 13, 14]
    assert svc.ctx.db.call(repo.all_users) == [] and len(svc.runs()) == runs
    assert window.durations == [20]  # the default window, started once
    window.gate.set()
    outs = await asyncio.gather(*tasks)
    assert all(o.kind is RequestKind.ISSUED and o.link for o in outs)  # type: ignore[attr-defined]
    assert len(svc.runs()) == runs + 1  # ONE apply for five users
    assert svc.fake.restart_count(RELAY) == relay + 1
    assert len(svc.ctx.db.call(repo.all_users)) == 5
    assert svc.ctx.db.call(repo.list_access_requests, "pending") == []
    links = {o.link for o in outs}  # type: ignore[attr-defined]
    assert len(links) == 5


async def test_a_new_window_starts_after_the_flush(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    window.gate.set()  # sleeping returns at once
    runs = len(svc.runs())
    a = await rs.submit(10, None, "A")
    b = await rs.submit(11, None, "B")
    assert a.kind is RequestKind.ISSUED and b.kind is RequestKind.ISSUED
    assert len(svc.runs()) == runs + 2  # separate windows -> separate applies
    assert window.durations == [20, 20]


async def test_failed_batch_leaves_requests_pending_and_a_retry_works(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    svc.fake.fail_on("systemctl", f"restart {RELAY}")
    tasks = [asyncio.ensure_future(rs.submit(20 + i, None, f"n{i}")) for i in range(3)]
    await window.started.wait()
    await asyncio.sleep(0.05)  # all three are queued in the window
    window.gate.set()
    outs = await asyncio.gather(*tasks)
    assert all(o.kind is RequestKind.FAILED and o.link is None and o.error for o in outs)
    assert svc.ctx.db.call(repo.all_users) == []
    assert len(await rs.list_pending()) == 3
    retry = await asyncio.gather(*(rs.submit(20 + i, None, f"n{i}") for i in range(3)))
    assert len(svc.ctx.db.call(repo.all_users)) == 3
    assert all(o.kind is RequestKind.ISSUED and o.link for o in retry)


async def test_same_display_name_in_one_batch_gets_distinct_names(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    tasks = [asyncio.ensure_future(rs.submit(tg, None, "Same")) for tg in (30, 31)]
    await window.started.wait()
    await asyncio.sleep(0.05)
    window.gate.set()
    outs = await asyncio.gather(*tasks)
    assert len({o.link for o in outs}) == 2
    assert all(o.kind is RequestKind.ISSUED for o in outs)
    names = sorted(u.name for u in svc.ctx.db.call(repo.all_users))
    assert len(set(names)) == 2 and names[0].startswith("Same")


async def test_default_hourly_limit_is_six(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    window.gate.set()
    outs = await asyncio.gather(*(rs.submit(100 + i, None, f"c{i}") for i in range(9)))
    assert sum(o.kind is RequestKind.ISSUED for o in outs) == 6
    assert sum(o.kind is RequestKind.CREATED for o in outs) == 3
    assert len(svc.ctx.db.call(repo.all_users)) == 6
    svc.clock.now += timedelta(hours=2)
    assert (await rs.submit(200, None, "later")).kind is RequestKind.ISSUED


async def test_zero_window_issues_at_once(svc: Svc, open_rs: tuple[RequestService, Window]) -> None:
    rs, window = open_rs
    svc.ctx.db.call(repo.set_setting, "open_mode_batch_window_s", "0")
    out = await rs.submit(10, None, "Bob")
    assert out.kind is RequestKind.ISSUED and window.durations == []


async def test_already_decided_and_existing_users_inside_a_batch(
    svc: Svc, open_rs: tuple[RequestService, Window]
) -> None:
    rs, window = open_rs
    window.gate.set()
    first = await rs.submit(10, None, "Bob")
    assert first.kind is RequestKind.ISSUED
    again = await rs.submit(10, None, "Bob")
    assert again.kind is RequestKind.HAS_ACCESS
