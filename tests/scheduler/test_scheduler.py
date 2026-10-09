from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tests.apply.conftest import SECRET_RE, T0
from tests.bot.helpers import FakeNotifier, FakeSender, forbidden
from tests.services.conftest import Svc
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.scheduler.scheduler import Scheduler
from tgpanel.services.api import NewUser
from tgpanel.services.notifier import Messenger


class Now:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Rig:
    def __init__(self, svc: Svc) -> None:
        self.svc = svc
        self.now = Now(T0 + timedelta(hours=2))  # before the 03:00 backup hour
        self.sender = FakeSender()
        self.notifier = FakeNotifier()
        self.rollups: list[datetime] = []

    def build(self, **kw: object) -> Scheduler:
        async def rollup(now: datetime) -> None:
            self.rollups.append(now)

        return Scheduler(
            users=self.svc.users,
            pipeline=self.svc.ctx.pipeline,
            db=self.svc.ctx.db,
            messenger=Messenger(self.sender, self.svc.ctx.pipeline),
            notifier=self.notifier,
            maybe_rollup=rollup,
            clock=self.now,
            **kw,  # type: ignore[arg-type]
        )

    async def user(
        self,
        name: str,
        tg_id: int | None,
        expires_in: timedelta,
        *,
        started: bool = True,
        created_ago: timedelta = timedelta(days=30),
    ) -> int:
        res = await self.svc.users.create(
            [NewUser(name=name, tg_id=tg_id, expires_at=self.now.at + expires_in)],
            "web:admin",
        )
        assert res.ok, res.error
        uid = res.user_ids[0]
        # move the creation time into the past so that the term length is ``created_ago + ...``
        self.svc.ctx.db.call(repo.update_user, uid, bot_started=started, can_message=started)
        self.svc.ctx.db.call(
            lambda c: c.execute(
                "UPDATE users SET created_at = ? WHERE id = ?",
                (
                    (self.now.at - created_ago).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    uid,
                ),
            )
        )
        return uid

    def applies(self) -> int:
        return len(self.svc.runs())


@pytest.fixture
async def rig(svc: Svc) -> Rig:
    await svc.users.load_hostname()
    return Rig(svc)


async def test_expired_batch_is_one_apply_and_notices_sent(rig: Rig) -> None:
    a = await rig.user("a", 1, timedelta(hours=1))
    b = await rig.user("b", 2, timedelta(hours=2))
    c = await rig.user("c", None, timedelta(hours=1))
    d = await rig.user("d", 4, timedelta(days=20))
    e = await rig.user("e", 5, timedelta(hours=1), started=False)
    rig.now.at += timedelta(hours=3)
    sched = rig.build()
    before = rig.applies()
    await sched.expire_job(rig.now.at)
    assert rig.applies() == before + 1
    for uid in (a, b, c, e):
        assert (await rig.svc.users.get(uid)).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    assert (await rig.svc.users.get(d)).status is UserStatus.ACTIVE  # type: ignore[union-attr]
    assert sorted(s.chat_id for s in rig.sender.sent) == [1, 2]  # only those who can be messaged
    await sched.expire_job(rig.now.at)  # nothing left: no apply, no repeat notice
    assert rig.applies() == before + 1 and len(rig.sender.sent) == 2


async def test_forbidden_on_notice_clears_can_message(rig: Rig) -> None:
    a = await rig.user("a", 1, timedelta(hours=1))
    rig.sender.script[1] = [forbidden(1)]
    rig.now.at += timedelta(hours=2)
    await rig.build().expire_job(rig.now.at)
    extra = rig.svc.ctx.db.call(repo.get_user_extra, a)
    assert extra and not extra.can_message


async def test_reminder_sent_once_and_rearmed_after_extension(rig: Rig) -> None:
    uid = await rig.user("a", 1, timedelta(days=2))
    far = await rig.user("far", 2, timedelta(days=10))
    sched = rig.build()
    await sched.reminder_job(rig.now.at)
    assert [s.chat_id for s in rig.sender.sent] == [1]
    await sched.reminder_job(rig.now.at)
    rig.now.at += timedelta(hours=5)
    await sched.reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 1  # no repeats, even across a restart:
    again = rig.build()
    await again.reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 1
    del uid, far


async def test_reminder_rearm_when_close_again(rig: Rig) -> None:
    uid = await rig.user("a", 1, timedelta(days=2))
    sched = rig.build()
    await sched.reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 1
    await rig.svc.users.extend([uid], 30, "web:admin")
    rig.now.at += timedelta(days=31)  # inside the window of the new expiry date
    await sched.reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 2


async def test_no_reminder_for_one_day_term(rig: Rig) -> None:
    await rig.user("day", 1, timedelta(hours=20), created_ago=timedelta(hours=1))
    await rig.build().reminder_job(rig.now.at)
    assert rig.sender.sent == []


async def test_no_reminder_without_can_message_or_for_expired(rig: Rig) -> None:
    await rig.user("silent", 1, timedelta(days=2), started=False)
    await rig.build().reminder_job(rig.now.at)
    assert rig.sender.sent == []


async def test_reminder_days_setting(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(days=5))
    sched = rig.build()
    await sched.reminder_job(rig.now.at)
    assert rig.sender.sent == []
    rig.svc.ctx.db.call(repo.set_setting, "reminder_days", "6")
    await sched.reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 1


async def test_daily_backup_once_per_day_at_hour(rig: Rig) -> None:
    rig.now.at = T0.replace(hour=1)
    sched = rig.build()
    n = len(rig.svc.ctx.db.call(repo.list_backups))
    await sched.backup_job(rig.now.at)  # 01:00 UTC-ish: too early
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n
    rig.now.at = T0.replace(hour=4)
    await sched.backup_job(rig.now.at)
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 1
    await sched.backup_job(rig.now.at + timedelta(hours=1))
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 1
    await sched.backup_job(rig.now.at + timedelta(days=1))
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 2


async def test_backup_failure_notifies_and_retries_later(rig: Rig) -> None:
    rig.now.at = T0.replace(hour=4)
    sched = rig.build()
    pipeline = rig.svc.ctx.pipeline
    real = pipeline.create_backup

    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("disk full")

    pipeline.create_backup = boom  # type: ignore[method-assign,assignment]
    with pytest.raises(RuntimeError):
        await sched.backup_job(rig.now.at)
    assert rig.notifier.messages and "бэкап" in rig.notifier.messages[0]
    pipeline.create_backup = real  # type: ignore[method-assign]
    n = len(rig.svc.ctx.db.call(repo.list_backups))
    await sched.backup_job(rig.now.at + timedelta(minutes=5))  # still in the retry pause
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n
    await sched.backup_job(rig.now.at + timedelta(hours=2))
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 1


async def test_job_exception_is_isolated_and_logged_scrubbed(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "cd" * 16

    async def bad(now: datetime) -> None:
        raise RuntimeError(f"rollup exploded secret={secret}")

    sched = Scheduler(
        users=rig.svc.users,
        pipeline=rig.svc.ctx.pipeline,
        db=rig.svc.ctx.db,
        messenger=Messenger(rig.sender, rig.svc.ctx.pipeline),
        maybe_rollup=bad,
        clock=rig.now,
    )
    uid = await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    caplog.set_level(logging.WARNING)
    await sched.tick()  # rollup fails, expiry before it still ran, backup after it runs
    assert (await rig.svc.users.get(uid)).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    assert "rollup" in caplog.text and secret not in caplog.text
    assert not SECRET_RE.search(caplog.text)


async def test_expire_job_failure_does_not_stop_other_jobs(rig: Rig) -> None:
    rig.now.at = T0.replace(hour=4)
    sched = rig.build()

    async def boom(now: datetime) -> None:
        raise RuntimeError("x")

    rig.svc.users.expire_due = boom  # type: ignore[method-assign,assignment]
    n = len(rig.svc.ctx.db.call(repo.list_backups))
    await sched.tick()
    assert rig.rollups == [rig.now.at]
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 1


async def test_run_loop_catch_up_ticks_and_stops(rig: Rig) -> None:
    uid = await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)  # overdue at startup: catch-up
    ticks = 0
    stop = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        nonlocal ticks
        assert seconds == 60.0
        ticks += 1
        rig.now.at += timedelta(seconds=60)
        await asyncio.sleep(0)
        if ticks == 3:
            stop.set()
            await asyncio.sleep(10)  # blocks until cancelled by the stop event

    sched = rig.build(sleep=fake_sleep)
    await asyncio.wait_for(sched.run(stop), timeout=5)
    assert (await rig.svc.users.get(uid)).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    assert len(rig.rollups) >= 3


async def test_run_is_cancellable(rig: Rig) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(rig.build().run(stop))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


_ = (UTC, Callable)
