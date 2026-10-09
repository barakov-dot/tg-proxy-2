"""Outgoing Telegram messages seen from the service layer (no aiogram Bot in here).

``MessageSender`` is the one thing the bot package implements (real = aiogram Bot, tests = fake).
``Messenger`` adds the delivery rules: RetryAfter -> sleep and retry, 403 -> can_message=false.
``Notifier`` messages admins; ``make_on_failure`` turns it into the ApplyPipeline failure hook.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from tgpanel.apply.pipeline import ApplyFailure, ApplyPipeline
from tgpanel.db.connection import transaction
from tgpanel.system.validation import scrub

log = logging.getLogger("tgpanel.notifier")

SENT = "sent"
FORBIDDEN = "forbidden"
ERROR_PREFIX = "error:"


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
        f"Причина: {scrub(failure.reason, 120) or 'не указана'}",
        f"Инициатор: {scrub(failure.actor, 60)}",
        scrub(failure.error, 600),
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
