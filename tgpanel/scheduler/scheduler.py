"""In-process scheduler (PLAN 6.1, 7): expiry, reminders, rollup hook, daily backup."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import read_settings
from tgpanel.bot import texts
from tgpanel.bot.format import fmt_dt
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.expiry import REMINDER_DAYS_BEFORE, select_reminders
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import UserService
from tgpanel.services.notifier import Messenger, Notifier
from tgpanel.system.validation import scrub

log = logging.getLogger("tgpanel.scheduler")

KEY_REMINDED = "scheduler.reminded"  # JSON {user_id: expires_at iso}
KEY_REMINDER_DAYS = "reminder_days"
KEY_LAST_BACKUP_DAY = "scheduler.last_backup_day"
KEY_BACKUP_HOUR = "backup_hour"
DEFAULT_BACKUP_HOUR = 3


class Scheduler:
    def __init__(
        self,
        *,
        users: UserService,
        pipeline: ApplyPipeline,
        db: Database,
        messenger: Messenger,
        notifier: Notifier | None = None,
        maybe_rollup: Callable[[datetime], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_s: float = 60.0,
        reminder_every_s: float = 600.0,
        backup_retry_s: float = 3600.0,
    ) -> None:
        self._users = users
        self._pipeline = pipeline
        self._db = db
        self._messenger = messenger
        self._notifier = notifier
        self._rollup = maybe_rollup
        self._clock = clock or pipeline.now
        self._sleep = sleep
        self._tick_s = tick_s
        self._reminder_every = reminder_every_s
        self._backup_retry = backup_retry_s
        self._last_reminder: datetime | None = None
        self._backup_retry_at: datetime | None = None

    # ------------------------------------------------------------------ loop

    async def run(self, stop_event: asyncio.Event) -> None:
        await self.tick()  # startup catch-up: everything overdue runs at once
        while not stop_event.is_set():
            waiter = asyncio.create_task(stop_event.wait())
            nap = asyncio.create_task(self._nap())
            try:
                await asyncio.wait({waiter, nap}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                nap.cancel()
                await asyncio.gather(waiter, nap, return_exceptions=True)
            if stop_event.is_set():
                break
            await self.tick()

    async def _nap(self) -> None:
        await self._sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock()
        await self._safe("expire", self.expire_job(now))
        if (
            self._last_reminder is None
            or (now - self._last_reminder).total_seconds() >= self._reminder_every
        ):
            self._last_reminder = now
            await self._safe("reminders", self.reminder_job(now))
        if self._rollup is not None:
            await self._safe("rollup", self._rollup(now))
        await self._safe("backup", self.backup_job(now))

    @staticmethod
    async def _safe(name: str, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("job %s failed: %s: %s", name, type(exc).__name__, scrub(str(exc), 200))

    # ------------------------------------------------------------------ expiry

    async def expire_job(self, now: datetime) -> None:
        res = await self._users.expire_due(now)  # ONE apply for all expired
        if not res.ok:
            log.warning("expiry apply failed")
            return
        for uid in res.user_ids:
            user = await self._db.run(repo.get_user, uid)
            extra = await self._db.run(repo.get_user_extra, uid)
            if user is None or extra is None or user.tg_id is None or not extra.can_message:
                continue
            await self._messenger.deliver(user.tg_id, texts.expiry_notice())

    # ------------------------------------------------------------------ reminders

    async def reminder_job(self, now: datetime) -> None:
        def load(conn: sqlite3.Connection) -> tuple[list[Any], dict[int, str], int, str]:
            cfg = read_settings(conn)
            days_raw = repo.get_setting(conn, KEY_REMINDER_DAYS, str(REMINDER_DAYS_BEFORE))
            days = int(days_raw) if days_raw and days_raw.isdigit() else REMINDER_DAYS_BEFORE
            try:
                reminded = {
                    int(k): str(v)
                    for k, v in json.loads(
                        repo.get_setting(conn, KEY_REMINDED, "{}") or "{}"
                    ).items()
                }
            except (ValueError, AttributeError):
                reminded = {}
            items = []
            for u in repo.all_users(conn):
                if u.status is UserStatus.ACTIVE and u.expires_at and u.tg_id is not None:
                    extra = repo.get_user_extra(conn, u.id)
                    if extra is not None and extra.can_message:
                        items.append((u, extra.created_at))
            return items, reminded, days, cfg.timezone

        items, reminded, days, tz = await self._db.run(load)
        done = {
            u.id
            for u, _ in items
            if u.expires_at is not None and reminded.get(u.id) == u.expires_at.isoformat()
        }
        due = select_reminders(items, now, days_before=days, already_reminded=done)
        if not due and not reminded:
            return
        new = {k: v for k, v in reminded.items() if k in {u.id for u, _ in items}}
        for user in due:
            assert user.tg_id is not None and user.expires_at is not None  # noqa: S101
            new[user.id] = user.expires_at.isoformat()  # at most once per expiry date
            await self._messenger.deliver(
                user.tg_id, texts.reminder_notice(fmt_dt(user.expires_at, tz))
            )
        if new != reminded:
            payload = json.dumps({str(k): v for k, v in new.items()})

            def save(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    repo.set_setting(conn, KEY_REMINDED, payload)

            await self._pipeline.db_write(save)

    # ------------------------------------------------------------------ backup

    async def backup_job(self, now: datetime) -> None:
        def load(conn: sqlite3.Connection) -> tuple[str, str | None, int]:
            cfg = read_settings(conn)
            hour = repo.get_setting(conn, KEY_BACKUP_HOUR, str(DEFAULT_BACKUP_HOUR)) or ""
            return (
                cfg.timezone,
                repo.get_setting(conn, KEY_LAST_BACKUP_DAY),
                int(hour) if hour.isdigit() and int(hour) < 24 else DEFAULT_BACKUP_HOUR,
            )

        tz, last_day, hour = await self._db.run(load)
        local = now.astimezone(ZoneInfo(tz))
        today = local.date().isoformat()
        if last_day == today or local.hour < hour:
            return
        if self._backup_retry_at is not None and now < self._backup_retry_at:
            return
        try:
            await self._pipeline.create_backup("daily", "system", full=True)
        except Exception as exc:
            self._backup_retry_at = datetime.fromtimestamp(
                now.timestamp() + self._backup_retry, UTC
            )
            if self._notifier is not None:
                await self._notifier.notify_admins(
                    "Ежедневный бэкап не создан: " + scrub(str(exc), 300)
                )
            raise
        self._backup_retry_at = None

        def save(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.set_setting(conn, KEY_LAST_BACKUP_DAY, today)

        await self._pipeline.db_write(save)
