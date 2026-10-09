"""Bot assembly: dispatcher with middlewares and routers, long-polling runner."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, LinkPreviewOptions

from tgpanel.bot import admin, texts, user
from tgpanel.bot.deps import BotDeps
from tgpanel.bot.middleware import LogMiddleware, PrivateOnlyMiddleware, ThrottleMiddleware
from tgpanel.bot.sender import AiogramSender
from tgpanel.services.notifier import scrub_secrets

log = logging.getLogger("tgpanel.bot")


def create_bot(token: str, session: BaseSession | None = None) -> Bot:
    """HTML parse mode, link previews off. ``session`` is for tests (never the network)."""
    return Bot(
        token,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )


def build_dispatcher(deps: BotDeps) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), deps=deps)
    throttle = ThrottleMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(PrivateOnlyMiddleware())
        observer.outer_middleware(throttle)
        observer.outer_middleware(LogMiddleware())
    for module_router in (admin.router, user.router):
        module_router._parent_router = None  # module-level routers; allow several dispatchers
    dp.include_router(admin.router)  # before the user router: its fallback catches the rest
    dp.include_router(user.router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        # never log the update: it carries message texts and links
        log.warning(
            "handler failed: %s: %s",
            type(event.exception).__name__,
            scrub_secrets(str(event.exception), 200),
        )
        update = event.update
        try:
            if update.callback_query is not None:
                await update.callback_query.answer(texts.ERROR_GENERIC, show_alert=True)
            elif update.message is not None:
                await update.message.answer(texts.ERROR_GENERIC)
        except Exception:
            log.warning("error reply failed")
        return True

    return dp


async def run_bot(
    token: str,
    deps: BotDeps,
    sender: AiogramSender,
    stop_event: asyncio.Event,
    *,
    session: BaseSession | None = None,
) -> None:
    """Long polling until ``stop_event`` is set. ``sender`` is bound to the running Bot."""
    bot = create_bot(token, session)
    sender.bind(bot)
    dp = build_dispatcher(deps)
    polling = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False, allowed_updates=["message", "callback_query"])
    )
    stopper = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not polling.done():
            await dp.stop_polling()
            await asyncio.gather(polling, return_exceptions=True)
        stopper.cancel()
        await deps.shutdown()
        sender.bind(None)
        await bot.session.close()
    if polling.done() and not polling.cancelled() and polling.exception() is not None:
        raise polling.exception()  # type: ignore[misc]
