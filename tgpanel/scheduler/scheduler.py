"""In-process scheduler (PLAN 6.1, 7): expiry, reminders, rollup hook, daily backup.

Delivery rules: an expiry notice / reminder counts as done only after ``sent`` or ``forbidden``
(an ``error:*`` result is retried, at most ``MAX_ATTEMPTS`` times) and the mark is persisted
right after each recipient. Expiry notices are queued in the settings table, so they survive
a restart and a bot that is not running yet (``ready`` predicate).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import read_settings
from tgpanel.bot import texts
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.expiry import REMINDER_DAYS_BEFORE, select_reminders
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import UserService
from tgpanel.services.notifier import (
    FORBIDDEN,
    SENT,
    Messenger,
    Notifier,
    Templates,
    message_values,
    scrub_secrets,
)

log = logging.getLogger("tgpanel.scheduler")

KEY_REMINDED = "scheduler.reminded"  # JSON {user_id: expires_at iso}
KEY_EXPIRY_NOTICES = "scheduler.expiry_notices"  # JSON {user_id: failed attempts}
KEY_REMINDER_DAYS = "reminder_days"
KEY_LAST_BACKUP_DAY = "scheduler.last_backup_day"
KEY_BACKUP_HOUR = "backup_hour"
DEFAULT_BACKUP_HOUR = 3
MAX_ATTEMPTS = 5
MAX_BACKOFF_MINUTES = 30


def _final(result: str) -> bool:
    return result in (SENT, FORBIDDEN)


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
        ready: Callable[[], bool] | None = None,
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
        self._ready = ready
        self._templates = Templates(db)
        self._clock = clock or pipeline.now
        self._sleep = sleep
        self._tick_s = tick_s
        self._reminder_every = reminder_every_s
        self._backup_retry = backup_retry_s
        self._last_reminder: datetime | None = None
        self._backup_retry_at: datetime | None = None
        self._last_backup_day: str | None = None  # in-memory twin of the persisted mark
        self._expire_streak = 0
        self._expire_next_at: datetime | None = None
        self._reminder_failures: dict[tuple[int, str], int] = {}

    def _is_ready(self) -> bool:
        return self._ready is None or self._ready()

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
        due = (
            self._last_reminder is None
            or (now - self._last_reminder).total_seconds() >= self._reminder_every
        )
        if due and self._is_ready():
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
            log.warning(
                "job %s failed: %s: %s", name, type(exc).__name__, scrub_secrets(str(exc), 200)
            )

    async def _notify(self, text: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.notify_admins(text)
        except Exception as exc:
            log.warning("admin notification failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ expiry

    async def expire_job(self, now: datetime) -> None:
        if self._expire_next_at is None or now >= self._expire_next_at:
            await self._expire_apply(now)
        if self._is_ready():
            await self._send_expiry_notices(now)

    async def _expire_apply(self, now: datetime) -> None:
        error: str | None = None
        user_ids: tuple[int, ...] = ()
        try:
            res = await self._users.expire_due(now)  # ONE apply for all expired
            if res.ok:
                user_ids = res.user_ids
            else:
                error = scrub_secrets(res.error or "ошибка применения", 200)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
        if error is not None:
            self._expire_streak += 1
            wait = min(2 ** (self._expire_streak - 1), MAX_BACKOFF_MINUTES)
            self._expire_next_at = now + timedelta(minutes=wait)
            log.warning(
                "expiry apply failed (streak %d), retry in %d min", self._expire_streak, wait
            )
            if self._expire_streak == 1:  # once per failure streak
                await self._notify(
                    "Автоматическое отключение пользователей с истёкшим сроком не удалось: "
                    f"{error}. Повторные попытки с нарастающей паузой."
                )
            return
        self._expire_streak = 0
        self._expire_next_at = None
        if user_ids:
            await self._queue_notices(user_ids)

    async def _save_setting(self, key: str, payload: str) -> None:
        def save(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.set_setting(conn, key, payload)

        await self._pipeline.db_write(save)

    async def _load_notices(self) -> dict[int, int]:
        raw = await self._db.run(repo.get_setting, KEY_EXPIRY_NOTICES, "{}")
        try:
            return {int(k): int(v) for k, v in json.loads(raw or "{}").items()}
        except (ValueError, AttributeError, TypeError):
            return {}

    async def _queue_notices(self, user_ids: tuple[int, ...]) -> None:
        pending = await self._load_notices()
        for uid in user_ids:
            pending.setdefault(uid, 0)
        await self._save_setting(
            KEY_EXPIRY_NOTICES, json.dumps({str(k): v for k, v in pending.items()})
        )

    async def _send_expiry_notices(self, now: datetime) -> None:
        pending = await self._load_notices()
        if not pending:
            return
        tz = (await self._db.run(read_settings)).timezone
        for uid in sorted(pending):
            user = await self._db.run(repo.get_user, uid)
            extra = await self._db.run(repo.get_user_extra, uid)
            if user is None or extra is None or user.tg_id is None or not extra.can_message:
                del pending[uid]  # nobody to tell
            else:
                try:
                    link, tg_link = self._users.link(user), self._users.tg_link(user)
                except Exception:
                    link = tg_link = ""
                text = await self._templates.render(
                    "msg.expired",
                    texts.DEFAULT_EXPIRED,
                    message_values(user, link, tg_link, tz, now),
                )
                result = await self._messenger.deliver(user.tg_id, text)
                if _final(result):
                    del pending[uid]
                else:
                    pending[uid] += 1
                    if pending[uid] >= MAX_ATTEMPTS:
                        del pending[uid]
            await self._save_setting(
                KEY_EXPIRY_NOTICES, json.dumps({str(k): v for k, v in pending.items()})
            )

    # ------------------------------------------------------------------ reminders

    async def reminder_job(self, now: datetime) -> None:
        def load(conn: sqlite3.Connection) -> tuple[list[Any], dict[int, str], int, str]:
            cfg = read_settings(conn)
            days_raw = repo.get_setting(conn, KEY_REMINDER_DAYS, str(REMINDER_DAYS_BEFORE))
            days = (
                int(days_raw)
                if days_raw and days_raw.isascii() and days_raw.isdigit()
                else REMINDER_DAYS_BEFORE
            )
            try:
                stored = json.loads(repo.get_setting(conn, KEY_REMINDED, "{}") or "{}")
                reminded = {int(k): str(v) for k, v in stored.items()}
            except (ValueError, AttributeError, TypeError):
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
        known = {u.id for u, _ in items}
        pruned = {k: v for k, v in reminded.items() if k in known}
        if pruned != reminded:
            reminded = pruned
            await self._save_setting(
                KEY_REMINDED, json.dumps({str(k): v for k, v in reminded.items()})
            )
        for user in due:
            if user.tg_id is None or user.expires_at is None:
                continue
            stamp = user.expires_at.isoformat()
            if self._reminder_failures.get((user.id, stamp), 0) >= MAX_ATTEMPTS:
                continue
            try:
                link, tg_link = self._users.link(user), self._users.tg_link(user)
            except Exception:
                link = tg_link = ""
            text = await self._templates.render(
                "msg.expiring",
                texts.DEFAULT_EXPIRING,
                message_values(user, link, tg_link, tz, now),
            )
            result = await self._messenger.deliver(user.tg_id, text)
            if _final(result):
                reminded[user.id] = stamp  # persisted right away: never twice
                await self._save_setting(
                    KEY_REMINDED, json.dumps({str(k): v for k, v in reminded.items()})
                )
            else:
                key = (user.id, stamp)
                self._reminder_failures[key] = self._reminder_failures.get(key, 0) + 1

    # ------------------------------------------------------------------ backup

    async def backup_job(self, now: datetime) -> None:
        def load(conn: sqlite3.Connection) -> tuple[str, str | None, int]:
            cfg = read_settings(conn)
            hour = repo.get_setting(conn, KEY_BACKUP_HOUR, str(DEFAULT_BACKUP_HOUR)) or ""
            valid = hour.isascii() and hour.isdigit() and int(hour) < 24
            return (
                cfg.timezone,
                repo.get_setting(conn, KEY_LAST_BACKUP_DAY),
                int(hour) if valid else DEFAULT_BACKUP_HOUR,
            )

        tz, last_day, hour = await self._db.run(load)
        local = now.astimezone(ZoneInfo(tz))
        today = local.date().isoformat()
        if today in (last_day, self._last_backup_day) or local.hour < hour:
            return
        if self._backup_retry_at is not None and now < self._backup_retry_at:
            return
        try:
            await self._pipeline.create_backup("daily", "system", full=True)
        except Exception as exc:
            self._backup_retry_at = datetime.fromtimestamp(
                now.timestamp() + self._backup_retry, UTC
            )
            await self._notify("Ежедневный бэкап не создан: " + scrub_secrets(str(exc), 300))
            raise
        self._backup_retry_at = None
        self._last_backup_day = today  # even if the persisted mark below cannot be written
        try:
            await self._save_setting(KEY_LAST_BACKUP_DAY, today)
        except Exception as exc:
            log.warning("backup mark was not saved: %s", type(exc).__name__)
