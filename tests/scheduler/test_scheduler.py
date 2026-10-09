from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

from tests.apply.conftest import SECRET_RE, T0
from tests.bot.helpers import FakeNotifier, FakeSender, forbidden
from tests.services.conftest import Svc
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.scheduler.scheduler import Scheduler
from tgpanel.services.api import NewUser
from tgpanel.services.notifier import Messenger, RateLimiter


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
        self.messenger = Messenger(self.sender, svc.ctx.pipeline)

    def build(self, **kw: object) -> Scheduler:
        async def rollup(now: datetime) -> None:
            self.rollups.append(now)

        return Scheduler(
            users=self.svc.users,
            pipeline=self.svc.ctx.pipeline,
            db=self.svc.ctx.db,
            messenger=self.messenger,
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


# ------------------------------------------------------------------ review fixes


async def _fail_expire(rig: Rig, sched: Scheduler, calls: list[datetime]) -> None:
    async def failing(now: datetime) -> object:
        calls.append(now)
        from tgpanel.services.api import OperationResult

        return OperationResult(ok=False, error="apply failed secret=" + "ab" * 16)

    rig.svc.users.expire_due = failing  # type: ignore[method-assign,assignment]


async def test_expire_failure_backoff_and_single_notification(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    sched = rig.build()
    calls: list[datetime] = []
    await _fail_expire(rig, sched, calls)
    gaps = []
    last = None
    for _ in range(60 * 3):  # three hours of one-minute ticks
        before = len(calls)
        await sched.expire_job(rig.now.at)
        if len(calls) > before:
            if last is not None:
                gaps.append((rig.now.at - last).total_seconds() / 60)
            last = rig.now.at
        rig.now.at += timedelta(minutes=1)
    assert gaps[:6] == [1, 2, 4, 8, 16, 30]
    assert all(g == 30 for g in gaps[5:])
    assert len(calls) < 15  # not 180 applies
    assert len(rig.notifier.messages) == 1  # once per failure streak
    assert "ab" * 16 not in rig.notifier.messages[0]


async def test_expire_backoff_resets_after_success(rig: Rig) -> None:
    uid = await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    sched = rig.build()
    real = rig.svc.users.expire_due
    calls: list[datetime] = []
    await _fail_expire(rig, sched, calls)
    await sched.expire_job(rig.now.at)
    rig.svc.users.expire_due = real  # type: ignore[method-assign]
    rig.now.at += timedelta(minutes=1)
    await sched.expire_job(rig.now.at)
    assert (await rig.svc.users.get(uid)).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    # a new streak notifies again
    await rig.user("b", 2, timedelta(minutes=1))
    rig.now.at += timedelta(hours=1)
    await _fail_expire(rig, sched, calls)
    await sched.expire_job(rig.now.at)
    assert len(rig.notifier.messages) == 2


async def test_expire_exception_also_backs_off(rig: Rig) -> None:
    sched = rig.build()
    n = 0

    async def boom(now: datetime) -> None:
        nonlocal n
        n += 1
        raise RuntimeError("x")

    rig.svc.users.expire_due = boom  # type: ignore[method-assign,assignment]
    for _ in range(3):
        await sched.expire_job(rig.now.at)
    assert n == 1


async def test_notice_error_is_retried_then_marked_only_when_final(rig: Rig) -> None:
    a = await rig.user("a", 1, timedelta(hours=1))
    b = await rig.user("b", 2, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    rig.sender.script[1] = [RuntimeError("net down")]
    sched = rig.build()
    await sched.expire_job(rig.now.at)
    assert [s.chat_id for s in rig.sender.sent] == [2]  # a failed, b went through
    pending = await sched._load_notices()
    assert pending == {a: 1}  # b is marked right after delivery, a stays queued
    await sched.expire_job(rig.now.at)
    assert [s.chat_id for s in rig.sender.sent] == [2, 1]
    assert await sched._load_notices() == {}
    await sched.expire_job(rig.now.at)
    assert len(rig.sender.sent) == 2
    del b


async def test_notice_forbidden_is_final(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    rig.sender.script[1] = [forbidden(1)]
    sched = rig.build()
    await sched.expire_job(rig.now.at)
    assert await sched._load_notices() == {}


async def test_notice_gives_up_after_max_attempts(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    rig.sender.script[1] = [RuntimeError("bad") for _ in range(20)]
    sched = rig.build()
    for _ in range(8):
        await sched.expire_job(rig.now.at)
    assert await sched._load_notices() == {}
    assert len(rig.sender.script[1]) == 20 - 5


async def test_notice_progress_survives_a_crash_mid_batch(rig: Rig) -> None:
    for i in range(1, 4):
        await rig.user(f"u{i}", i, timedelta(hours=1))
    rig.now.at += timedelta(hours=2)
    rig.sender.script[2] = [asyncio.CancelledError()]  # process dies while sending #2
    sched = rig.build()
    with pytest.raises(asyncio.CancelledError):
        await sched.expire_job(rig.now.at)
    assert [s.chat_id for s in rig.sender.sent] == [1]
    # "restart": a new scheduler continues with the rest, nobody is told twice
    await rig.build().expire_job(rig.now.at)
    assert sorted(s.chat_id for s in rig.sender.sent) == [1, 2, 3]


async def test_reminder_error_is_not_marked_and_progress_persisted(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(days=2))
    await rig.user("b", 2, timedelta(days=2))
    rig.sender.script[1] = [RuntimeError("net")]
    sched = rig.build()
    await sched.reminder_job(rig.now.at)
    assert [s.chat_id for s in rig.sender.sent] == [2]
    await sched.reminder_job(rig.now.at)  # a is retried, b is not repeated
    assert [s.chat_id for s in rig.sender.sent] == [2, 1]
    await rig.build().reminder_job(rig.now.at)
    assert len(rig.sender.sent) == 2


async def test_reminder_progress_survives_crash_mid_batch(rig: Rig) -> None:
    await rig.user("a", 1, timedelta(days=2))
    await rig.user("b", 2, timedelta(days=2))
    rig.sender.script[2] = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await rig.build().reminder_job(rig.now.at)
    await rig.build().reminder_job(rig.now.at)
    assert sorted(s.chat_id for s in rig.sender.sent) == [1, 2]


async def test_notices_wait_for_the_bot_but_expiry_still_applies(rig: Rig) -> None:
    uid = await rig.user("a", 1, timedelta(hours=1))
    await rig.user("r", 2, timedelta(days=2))
    rig.now.at += timedelta(hours=2)
    ready = [False]
    sched = rig.build(ready=lambda: ready[0])
    await sched.tick()
    assert (await rig.svc.users.get(uid)).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    assert rig.sender.sent == [] and len(rig.rollups) == 1
    ready[0] = True
    rig.now.at += timedelta(minutes=1)
    await sched.tick()
    assert sorted(s.chat_id for s in rig.sender.sent) == [1, 2]  # the notice and the reminder


async def test_300_expiry_notices_respect_20_per_second(rig: Rig) -> None:
    from tests.bot.helpers import FakeTime

    t = FakeTime()
    rig.sender = FakeSender(time=t)
    rig.messenger = Messenger(
        rig.sender,
        rig.svc.ctx.pipeline,
        limiter=RateLimiter(20, clock=t.monotonic, sleep=t.sleep),
        sleep=t.sleep,
    )
    res = await rig.svc.users.create(
        [
            NewUser(name=f"m{i}", tg_id=5000 + i, expires_at=rig.now.at + timedelta(hours=1))
            for i in range(300)
        ],
        "web:admin",
    )
    assert res.ok
    for uid in res.user_ids:
        rig.svc.ctx.db.call(repo.update_user, uid, bot_started=True, can_message=True)
    rig.now.at += timedelta(hours=2)
    before = rig.applies()
    await rig.build().expire_job(rig.now.at)
    assert rig.applies() == before + 1  # still ONE apply for the batch
    times = [s.at for s in rig.sender.sent]
    assert len(times) == 300
    for i, at in enumerate(times):
        assert len([x for x in times[i:] if x < at + 1.0 - 1e-6]) <= 20
    assert times[-1] >= 14.9


async def test_backup_mark_failure_does_not_repeat_backups(rig: Rig) -> None:
    rig.now.at = T0.replace(hour=4)
    sched = rig.build()
    n = len(rig.svc.ctx.db.call(repo.list_backups))

    async def cannot_save(key: str, payload: str) -> None:
        raise RuntimeError("db busy")

    sched._save_setting = cannot_save  # type: ignore[method-assign]
    await sched.backup_job(rig.now.at)
    await sched.backup_job(rig.now.at + timedelta(minutes=1))
    await sched.backup_job(rig.now.at + timedelta(minutes=2))
    assert len(rig.svc.ctx.db.call(repo.list_backups)) == n + 1


async def test_templates_msg_expired_and_expiring_are_used(rig: Rig) -> None:
    rig.svc.ctx.db.call(repo.set_setting, "msg.expired", "Всё, {name} <b>")
    rig.svc.ctx.db.call(repo.set_setting, "msg.expiring", "{name}: {days} дн. до {expires}")
    await rig.user("gone", 1, timedelta(hours=1))
    await rig.user("soon", 2, timedelta(days=2))
    rig.now.at += timedelta(hours=2)
    sched = rig.build()
    await sched.expire_job(rig.now.at)
    await sched.reminder_job(rig.now.at)
    texts = {s.chat_id: s.text for s in rig.sender.sent}
    assert texts[1] == "Всё, gone &lt;b&gt;"
    assert texts[2].startswith("soon: 2 дн. до ")


async def test_job_log_masks_bot_tokens(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    token = "123456789:" + "Zz9_" * 9

    async def bad(now: datetime) -> None:
        raise RuntimeError(f"url /bot{token}/getMe")

    sched = Scheduler(
        users=rig.svc.users,
        pipeline=rig.svc.ctx.pipeline,
        db=rig.svc.ctx.db,
        messenger=rig.messenger,
        maybe_rollup=bad,
        clock=rig.now,
    )
    caplog.set_level(logging.WARNING)
    await sched.tick()
    assert token not in caplog.text and "[redacted]" in caplog.text
