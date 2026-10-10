"""Broadcast to bot users (PLAN 6.3): preview, rate-limited resumable send, per-item report.

Items live in ``broadcast_items``; ``result`` is one of ``pending``, ``sent``, ``forbidden``,
``error:<ExceptionName>``, ``skipped:<code>``. Only ``pending`` items are ever sent, so ``run``
may be repeated after a cancel or a crash (a message whose result could not be recorded after
a crash may be repeated once). Message texts and links are never stored or logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import read_settings
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.db.times import from_db_opt
from tgpanel.domain.models import UserStatus, shown_name
from tgpanel.services.api import UserService
from tgpanel.services.notifier import (
    FORBIDDEN,
    SENT,
    LinkButton,
    MessageSender,
    Messenger,
    RateLimiter,
    Templates,
    message_values,
    render_message,
)

log = logging.getLogger("tgpanel.broadcast")
MAX_TEMPLATE = 3000
RATE_PER_SECOND = 20.0
BUTTON_TEXT = "Подключиться"
SKIP_PREFIX = "skipped:"

EXCLUSION_TEXT = {
    "no_tg_id": "не указан Telegram ID",
    "not_active": "профиль отключён или срок истёк",
    "bot_not_started": "бот не запущен",
    "cannot_message": "бот заблокирован пользователем",
    "user_deleted": "пользователь удалён",
}


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: int
    name: str
    tg_id: int | None
    reason: str | None  # exclusion code (key of EXCLUSION_TEXT) or None = will receive

    @property
    def reason_text(self) -> str | None:
        return None if self.reason is None else EXCLUSION_TEXT[self.reason]


@dataclass(frozen=True, slots=True)
class BroadcastPreview:
    recipients: tuple[Recipient, ...]

    @property
    def included(self) -> tuple[Recipient, ...]:
        return tuple(r for r in self.recipients if r.reason is None)

    @property
    def excluded(self) -> tuple[Recipient, ...]:
        return tuple(r for r in self.recipients if r.reason is not None)


@dataclass(frozen=True, slots=True)
class ReportItem:
    user_id: int | None
    name: str | None
    tg_id: int | None
    result: str

    @property
    def result_text(self) -> str:
        if self.result.startswith(SKIP_PREFIX):
            return "пропущен: " + EXCLUSION_TEXT.get(self.result[len(SKIP_PREFIX) :], "?")
        return {
            "pending": "ожидает отправки",
            SENT: "отправлено",
            FORBIDDEN: "бот заблокирован",
        }.get(self.result, "ошибка" if self.result.startswith("error:") else self.result)


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    broadcast_id: int
    created_by: str
    created_at: datetime | None
    template: str
    items: tuple[ReportItem, ...]

    def _count(self, pred: Callable[[str], bool]) -> int:
        return sum(1 for i in self.items if pred(i.result))

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def sent(self) -> int:
        return self._count(lambda r: r == SENT)

    @property
    def forbidden(self) -> int:
        return self._count(lambda r: r == FORBIDDEN)

    @property
    def errors(self) -> int:
        return self._count(lambda r: r.startswith("error:"))

    @property
    def skipped(self) -> int:
        return self._count(lambda r: r.startswith(SKIP_PREFIX))

    @property
    def pending(self) -> int:
        return self._count(lambda r: r == "pending")


def check_template(template: str) -> str:
    text = template.strip()
    if not text:
        raise OperationRejected("Пустой текст рассылки")
    if len(text) > MAX_TEMPLATE:
        raise OperationRejected(f"Текст длиннее {MAX_TEMPLATE} символов")
    return text


def render_template(template: str, values: dict[str, str]) -> str:
    """HTML-safe text: template and values are escaped; only the known placeholders expand."""
    return render_message(template, values)


class BroadcastService:
    def __init__(
        self,
        pipeline: ApplyPipeline,
        db: Database,
        users: UserService,
        sender: MessageSender,
        *,
        messenger: Messenger | None = None,
        rate: float = RATE_PER_SECOND,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pipeline = pipeline
        self._db = db
        self._users = users
        # Pass the application-wide Messenger so that broadcasts, notices and reminders share
        # ONE rate limiter (<= 20 messages per second in total).
        self._messenger = messenger or Messenger(
            sender,
            pipeline,
            limiter=RateLimiter(rate, clock=monotonic, sleep=sleep),
            sleep=sleep,
        )
        self._cancelled: set[int] = set()
        self._running: dict[int, asyncio.Lock] = {}

    def is_running(self, broadcast_id: int) -> bool:
        return broadcast_id in self._running

    # ------------------------------------------------------------------ preview / create

    async def preview(self, user_ids: Sequence[int] | None = None) -> BroadcastPreview:
        """Recipients for ``user_ids`` (None = everybody) with exclusion reasons."""

        def load(conn: sqlite3.Connection) -> list[Recipient]:
            if user_ids is None:
                users = repo.all_users(conn)
            else:
                users = repo.users_by_ids(conn, list(dict.fromkeys(user_ids)))
            out: list[Recipient] = []
            for u in users:
                extra = repo.get_user_extra(conn, u.id)
                reason: str | None = None
                if u.tg_id is None:
                    reason = "no_tg_id"
                elif u.status is not UserStatus.ACTIVE:
                    reason = "not_active"
                elif extra is None or not extra.bot_started:
                    reason = "bot_not_started"
                elif not extra.can_message:
                    reason = "cannot_message"
                out.append(Recipient(u.id, shown_name(u), u.tg_id, reason))
            return out

        return BroadcastPreview(tuple(await self._db.run(load)))

    async def create(self, template: str, actor: str, user_ids: Sequence[int] | None = None) -> int:
        """Write the broadcast and one item per recipient (excluded ones as ``skipped:``)."""
        text = check_template(template)
        preview = await self.preview(user_ids)
        if not preview.recipients:
            raise OperationRejected("Некому отправлять")

        def write(conn: sqlite3.Connection) -> int:
            with transaction(conn):
                now = self._pipeline.now()
                bid = repo.create_broadcast(conn, actor, now, text)
                for r in preview.recipients:
                    item = repo.add_broadcast_item(conn, bid, r.user_id, r.tg_id)
                    if r.reason is not None:
                        repo.set_broadcast_item_result(conn, item, SKIP_PREFIX + r.reason, None)
                repo.add_audit(
                    conn,
                    now,
                    actor,
                    "broadcast.create",
                    f"broadcast:{bid}",
                    f"total={len(preview.recipients)} send={len(preview.included)}",
                )
                return bid

        return await self._pipeline.db_write(write)

    async def start(
        self,
        template: str,
        actor: str,
        user_ids: Sequence[int] | None = None,
        *,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> BroadcastReport:
        bid = await self.create(template, actor, user_ids)
        return await self.run(bid, on_progress=on_progress)

    # ------------------------------------------------------------------ send

    def cancel(self, broadcast_id: int) -> None:
        """Stop after the current message; pending items stay pending (``run`` resumes them)."""
        self._cancelled.add(broadcast_id)

    async def run(
        self,
        broadcast_id: int,
        *,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> BroadcastReport:
        if broadcast_id in self._running:
            return await self.report(broadcast_id)
        lock = self._running[broadcast_id] = asyncio.Lock()
        try:
            await self._run_locked(lock, broadcast_id, on_progress)
        finally:
            self._running.pop(broadcast_id, None)
        return await self.report(broadcast_id)

    async def _run_locked(
        self,
        lock: asyncio.Lock,
        broadcast_id: int,
        on_progress: Callable[[int, int], Awaitable[None]] | None,
    ) -> None:
        async with lock:
            self._cancelled.discard(broadcast_id)
            cfg = await self._db.run(read_settings)
            zone = ZoneInfo(cfg.timezone)
            rows = await self._db.run(repo.list_broadcast_items, broadcast_id)
            template = await self._db.run(self._template, broadcast_id)
            todo = [
                (int(r["id"]), r["user_id"], r["tg_id"]) for r in rows if r["result"] == "pending"
            ]
            total = len(todo)
            for done, (item_id, user_id, tg_id) in enumerate(todo):
                if broadcast_id in self._cancelled:
                    break
                result = await self._send_one(template, user_id, tg_id, zone)
                await self._record(item_id, result)
                if on_progress is not None:
                    await on_progress(done + 1, total)
            self._cancelled.discard(broadcast_id)

    @staticmethod
    def _template(conn: sqlite3.Connection, broadcast_id: int) -> str:
        row = conn.execute(
            "SELECT template FROM broadcasts WHERE id = ?", (broadcast_id,)
        ).fetchone()
        if row is None:
            raise OperationRejected("Рассылка не найдена")
        return str(row["template"])

    async def _send_one(
        self, template: str, user_id: int | None, tg_id: int | None, zone: ZoneInfo
    ) -> str:
        user = None if user_id is None else await self._db.run(repo.get_user, user_id)
        extra = None if user_id is None else await self._db.run(repo.get_user_extra, user_id)
        if user is None or extra is None:
            return SKIP_PREFIX + "user_deleted"
        if user.tg_id is None or tg_id is None:
            return SKIP_PREFIX + "no_tg_id"
        if user.status is not UserStatus.ACTIVE:
            return SKIP_PREFIX + "not_active"
        if not extra.can_message:
            return SKIP_PREFIX + "cannot_message"
        try:
            link = self._users.link(user)
            tg_link = self._users.tg_link(user)
        except Exception:
            return "error:NoLink"
        text = render_message(
            template, message_values(user, link, tg_link, zone.key, self._pipeline.now())
        )
        return await self._messenger.deliver(tg_id, text, LinkButton(BUTTON_TEXT, link))

    async def _record(self, item_id: int, result: str) -> None:
        sent_at = self._pipeline.now() if result == SENT else None

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.set_broadcast_item_result(conn, item_id, result, sent_at)

        await self._pipeline.db_write(write)

    # ------------------------------------------------------------------ report

    async def report(self, broadcast_id: int) -> BroadcastReport:
        def load(conn: sqlite3.Connection) -> BroadcastReport:
            head = conn.execute(
                "SELECT created_by, created_at, template FROM broadcasts WHERE id = ?",
                (broadcast_id,),
            ).fetchone()
            if head is None:
                raise OperationRejected("Рассылка не найдена")
            rows = conn.execute(
                "SELECT i.user_id, COALESCE(NULLIF(u.display_name, ''), u.name) AS name, i.tg_id,"
                " i.result FROM broadcast_items i"
                " LEFT JOIN users u ON u.id = i.user_id WHERE i.broadcast_id = ? ORDER BY i.id",
                (broadcast_id,),
            ).fetchall()
            return BroadcastReport(
                broadcast_id=broadcast_id,
                created_by=str(head["created_by"]),
                created_at=from_db_opt(head["created_at"]),
                template=str(head["template"]),
                items=tuple(
                    ReportItem(
                        None if r["user_id"] is None else int(r["user_id"]),
                        r["name"],
                        None if r["tg_id"] is None else int(r["tg_id"]),
                        str(r["result"]),
                    )
                    for r in rows
                ),
            )

        return await self._db.run(load)


# ------------------------------------------------------------------------ link delivery

KEY_PENDING_LINKS = "link.pending"  # JSON list of user ids waiting for their first /start
NOT_STARTED_NOTE = "бот не запущен у пользователя — ссылка будет отправлена при первом /start"
BLOCKED_NOTE = "пользователь заблокировал бота — ссылка будет отправлена после его /start"
ERROR_NOTE = "ошибка доставки"
_NO_RETRY = ("TelegramBadRequest", "TelegramUnauthorizedError", "TelegramNotFound")
LINK_STATUSES = ("sent", "pending", "blocked", "error", "skipped")


@dataclass(frozen=True, slots=True)
class LinkResult:
    user_id: int
    status: str  # one of LINK_STATUSES
    note: str = ""  # Russian, never contains the link

    @property
    def ok(self) -> bool:
        return self.status == "sent"


class LinkDelivery:
    """Sends a user their link through the shared Messenger, with retries and a pending flag.

    A user who has not started the bot (or blocked it) gets a "pending" flag; the link is
    delivered at their next /start. Delivery never raises: the caller learns the outcome from
    the result, and a failure is logged without the message or link.
    """

    def __init__(
        self,
        pipeline: ApplyPipeline,
        db: Database,
        users: UserService,
        messenger: Messenger,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retries: int = 3,
    ) -> None:
        self._pipeline = pipeline
        self._db = db
        self._users = users
        self._messenger = messenger
        self._sleep = sleep
        self._retries = retries
        self._templates = Templates(db)

    # ------------------------------------------------------------------ pending flags

    async def pending_ids(self) -> list[int]:
        raw = await self._db.run(repo.get_setting, KEY_PENDING_LINKS, "[]")
        return sorted(_parse_id_list(raw or "[]"))

    async def _edit_pending(self, user_id: int, add: bool) -> bool:
        """Set or clear one flag atomically; returns whether it was set before."""

        def work(conn: sqlite3.Connection) -> bool:
            with transaction(conn):
                ids = _parse_id_list(repo.get_setting(conn, KEY_PENDING_LINKS, "[]") or "[]")
                was = user_id in ids
                if was == add:
                    return was
                if add:
                    ids.add(user_id)
                else:
                    ids.discard(user_id)
                repo.set_setting(conn, KEY_PENDING_LINKS, json.dumps(sorted(ids)))
                return was

        return await self._pipeline.db_write(work)

    async def mark_pending(self, user_id: int) -> None:
        """Remember that this user still has to receive the link (delivered at /start)."""
        await self._edit_pending(user_id, True)

    async def take_pending(self, user_id: int) -> bool:
        """True (and the flag is cleared) if a link was waiting for this user's /start."""
        return await self._edit_pending(user_id, False)

    # ------------------------------------------------------------------ delivery

    async def deliver(
        self,
        user_ids: Sequence[int],
        actor: str,
        *,
        key: str = "msg.link",
        default: str,
    ) -> list[LinkResult]:
        tz = (await self._db.run(repo.get_setting, "timezone", "UTC")) or "UTC"
        out = [await self._one(uid, key, default, tz) for uid in dict.fromkeys(user_ids)]
        sent = sum(1 for r in out if r.ok)

        def audit(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.add_audit(
                    conn,
                    self._pipeline.now(),
                    actor,
                    "user.send_links",
                    f"users:{len(out)}",
                    f"sent={sent}",
                )

        try:
            await self._pipeline.db_write(audit)
        except Exception as exc:
            log.warning("link delivery audit failed: %s", type(exc).__name__)
        return out

    async def _one(self, uid: int, key: str, default: str, tz: str) -> LinkResult:
        user = await self._users.get(uid)
        extra = await self._db.run(repo.get_user_extra, uid)
        if user is None or extra is None:
            return LinkResult(uid, "skipped", "Пользователь не найден")
        if user.tg_id is None:
            return LinkResult(uid, "skipped", "Не указан Telegram ID")
        if user.status is not UserStatus.ACTIVE:
            return LinkResult(uid, "skipped", "Профиль отключён или срок истёк")
        adopt = False
        if not extra.bot_started or not extra.can_message:
            # A person who asked the bot for access has a chat with it even though they had no
            # profile (so no bot_started flag): try to deliver, and bind on success.
            known = not extra.bot_started and await self._had_request(user.tg_id)
            if not known:
                await self._edit_pending(uid, True)
                note = BLOCKED_NOTE if extra.bot_started else NOT_STARTED_NOTE
                return LinkResult(uid, "blocked" if extra.bot_started else "pending", note)
            adopt = True
        try:
            link, tg_link = self._users.link(user), self._users.tg_link(user)
        except Exception:
            return LinkResult(uid, "error", "Не задано имя хоста прокси")
        text = await self._templates.render(
            key, default, message_values(user, link, tg_link, tz, self._pipeline.now())
        )
        text = text.replace("{intro}", "")
        result = await self._send(user.tg_id, text, LinkButton(BUTTON_TEXT, link))
        if result == SENT:
            await self._edit_pending(uid, False)
            if adopt:
                await self._bind(uid)
            return LinkResult(uid, "sent")
        if result == FORBIDDEN:
            await self._edit_pending(uid, True)
            if adopt:
                return LinkResult(uid, "pending", NOT_STARTED_NOTE)
            return LinkResult(uid, "blocked", BLOCKED_NOTE)
        log.warning("link delivery failed for user %d: %s", uid, result)
        return LinkResult(uid, "error", ERROR_NOTE)

    async def _had_request(self, tg_id: int) -> bool:
        def check(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM access_requests WHERE tg_id = ? LIMIT 1", (tg_id,)
            ).fetchone()
            return row is not None

        return bool(await self._db.run(check))

    async def _bind(self, user_id: int) -> None:
        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.update_user(conn, user_id, bot_started=True, can_message=True)

        try:
            await self._pipeline.db_write(work)
        except Exception as exc:
            log.warning("bot_started was not recorded: %s", type(exc).__name__)

    async def _send(self, tg_id: int, text: str, button: LinkButton) -> str:
        result = ""
        for attempt in range(self._retries + 1):
            result = await self._messenger.deliver(tg_id, text, button)
            if result == SENT or result == FORBIDDEN:
                return result
            if result.removeprefix("error:") in _NO_RETRY or attempt == self._retries:
                break
            await self._sleep(2.0**attempt)  # transient (network, server): back off and retry
        return result


def _parse_id_list(raw: str) -> set[int]:
    try:
        data = json.loads(raw)
        return {int(i) for i in data if isinstance(i, int) and not isinstance(i, bool)}
    except (ValueError, TypeError):
        return set()
