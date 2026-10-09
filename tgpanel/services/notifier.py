"""Outgoing Telegram messages seen from the service layer (no aiogram Bot in here).

``MessageSender`` is the one thing the bot package implements (real = aiogram Bot, tests = fake).
``Messenger`` adds the delivery rules: RetryAfter -> sleep and retry, 403 -> can_message=false.
``Notifier`` messages admins; ``make_on_failure`` turns it into the ApplyPipeline failure hook.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from tgpanel.apply.pipeline import ApplyFailure, ApplyPipeline
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.models import UserRecord
from tgpanel.system.validation import scrub

log = logging.getLogger("tgpanel.notifier")

BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_PLACEHOLDER = re.compile(r"\{(name|link|tg_link|expires|days)\}")
TEMPLATE_KEYS = (
    "msg.link",
    "msg.welcome",
    "msg.approved",
    "msg.rejected",
    "msg.expiring",
    "msg.expired",
    "msg.broadcast",
)

SENT = "sent"
FORBIDDEN = "forbidden"
ERROR_PREFIX = "error:"


def scrub_secrets(text: str, limit: int = 700) -> str:
    """``scrub`` plus Telegram bot tokens (``123456789:AA...``)."""
    return scrub(BOT_TOKEN_RE.sub("[redacted]", text), limit)


def render_message(template: str, values: Mapping[str, str], *, escape: bool = True) -> str:
    """Safe formatter: only {name} {link} {tg_link} {expires} {days} expand; all else is literal.

    With ``escape`` (Telegram HTML mode) the template and the values are HTML-escaped.
    """

    def esc(text: str) -> str:
        return html.escape(text, quote=False) if escape else text

    return _PLACEHOLDER.sub(lambda m: esc(values.get(m.group(1), "")), esc(template))


def message_values(
    user: UserRecord, link: str, tg_link: str, tz: str, now: datetime
) -> dict[str, str]:
    expires = "без срока"
    days = ""
    if user.expires_at is not None:
        expires = user.expires_at.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")
        days = str(max(0, math.ceil((user.expires_at - now).total_seconds() / 86400)))
    return {"name": user.name, "link": link, "tg_link": tg_link, "expires": expires, "days": days}


class Templates:
    """Message texts edited on the web page (settings ``msg.*``), with code defaults."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def custom(self, key: str) -> str | None:
        raw = await self._db.run(repo.get_setting, key, "")
        return str(raw or "").strip() or None

    async def render(
        self, key: str, default: str, values: Mapping[str, str], *, escape: bool = True
    ) -> str:
        template = await self.custom(key) or default
        return render_message(template, values, escape=escape)


@dataclass(frozen=True, slots=True)
class LinkButton:
    text: str
    url: str


class MessageSender(Protocol):
    async def send_message(
        self, chat_id: int, text: str, *, button: LinkButton | None = None, html: bool = True
    ) -> None:
        """Raises aiogram's TelegramRetryAfter / TelegramForbiddenError / TelegramAPIError."""
        ...


class Notifier(Protocol):
    async def notify_admins(self, text: str) -> None:
        """Plain-text message to every admin; must never raise."""
        ...


class RateLimiter:
    """Spaces calls ``1/rate`` seconds apart (<= ``rate`` per second at any moment)."""

    def __init__(
        self,
        rate: float = 20.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    async def acquire(self) -> None:
        now = self._clock()
        at = max(now, self._next)
        self._next = at + self._interval
        if at > now:
            await self._sleep(at - now)


def _mark_blocked(conn: sqlite3.Connection, tg_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE users SET can_message = 0 WHERE tg_id = ?", (tg_id,))


class Messenger:
    """Delivers one message to a user and classifies the outcome (never raises Telegram errors)."""

    def __init__(
        self,
        sender: MessageSender,
        pipeline: ApplyPipeline,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 5,
    ) -> None:
        self._sender = sender
        self._pipeline = pipeline
        self._limiter = limiter
        self._sleep = sleep
        self._max_retries = max_retries

    async def mark_blocked(self, tg_id: int) -> None:
        await self._pipeline.db_write(_mark_blocked, tg_id)

    async def deliver(
        self, tg_id: int, text: str, button: LinkButton | None = None, *, html: bool = True
    ) -> str:
        """Returns ``sent`` | ``forbidden`` (can_message cleared) | ``error:<ExceptionName>``."""
        for _attempt in range(self._max_retries + 1):
            if self._limiter is not None:
                await self._limiter.acquire()
            try:
                await self._sender.send_message(tg_id, text, button=button, html=html)
            except TelegramRetryAfter as exc:
                await self._sleep(float(exc.retry_after))
                continue
            except TelegramForbiddenError:
                await self.mark_blocked(tg_id)
                return FORBIDDEN
            except Exception as exc:
                return ERROR_PREFIX + type(exc).__name__
            return SENT
        return ERROR_PREFIX + "TelegramRetryAfter"


class NullNotifier:
    async def notify_admins(self, text: str) -> None:
        return None


class LateBoundNotifier:
    """Placeholder usable before the bot exists (the pipeline is built first); ``bind`` later."""

    def __init__(self) -> None:
        self._target: Notifier | None = None

    def bind(self, target: Notifier | None) -> None:
        self._target = target

    async def notify_admins(self, text: str) -> None:
        if self._target is not None:
            await self._target.notify_admins(text)


def failure_text(failure: ApplyFailure) -> str:
    lines = [
        "Не удалось применить изменения.",
        f"Запуск: #{failure.run_id}",
        f"Причина: {scrub_secrets(failure.reason, 120) or 'не указана'}",
        f"Инициатор: {scrub_secrets(failure.actor, 60)}",
        scrub_secrets(failure.error, 600),
    ]
    if failure.rollback_errors:
        lines.append("ВНИМАНИЕ: откат выполнен не полностью, проверьте состояние сервисов.")
    elif failure.rolled_back:
        lines.append("Изменения откатены.")
    return "\n".join(lines)


def make_on_failure(notifier: Notifier) -> Callable[[ApplyFailure], Awaitable[None]]:
    """Hook for ``ApplyPipeline(on_failure=...)``: alerts admins, never raises, no secrets."""

    async def on_failure(failure: ApplyFailure) -> None:
        try:
            await notifier.notify_admins(failure_text(failure))
        except Exception:
            log.warning("admin failure notification failed")

    return on_failure
