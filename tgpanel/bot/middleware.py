"""Bot middlewares: private chats only, per-user throttle, content-free logging."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from tgpanel.bot import texts
from tgpanel.bot.deps import BotDeps

log = logging.getLogger("tgpanel.bot")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class PrivateOnlyMiddleware(BaseMiddleware):
    """Drops everything that does not happen in a private chat with a real user."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message is not None:
            chat = event.message.chat  # also for InaccessibleMessage (old messages)
        user = getattr(event, "from_user", None)
        if chat is None or chat.type != "private" or user is None or user.is_bot:
            return None
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Ignores events of one user that come faster than ``deps.throttle_interval``.

    The user is told "too fast" once per burst instead of being dropped silently.
    """

    def __init__(self) -> None:
        self._last: dict[int, float] = {}
        self._warned: set[int] = set()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        deps: BotDeps = data["deps"]
        user = getattr(event, "from_user", None)
        if user is not None and deps.throttle_interval > 0:
            now = deps.monotonic()
            last = self._last.get(user.id)
            if last is not None and now - last < deps.throttle_interval:
                await self._reject(event, user.id)
                return None
            self._last[user.id] = now
            self._warned.discard(user.id)
            if len(self._last) > 10_000:
                self._last = {k: v for k, v in self._last.items() if now - v < 60}
                self._warned &= set(self._last)
        return await handler(event, data)

    async def _reject(self, event: TelegramObject, user_id: int) -> None:
        first = user_id not in self._warned
        self._warned.add(user_id)
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(texts.TOO_FAST if first else None)
            elif isinstance(event, Message) and first:
                await event.answer(texts.TOO_FAST)
        except Exception as exc:
            log.warning("throttle reply failed: %s", type(exc).__name__)


class LogMiddleware(BaseMiddleware):
    """Logs only the kind of event and the sender id: never texts, callback data or links."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        log.debug("event %s from %s", type(event).__name__, getattr(user, "id", None))
        return await handler(event, data)
