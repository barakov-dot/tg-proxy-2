"""aiogram-backed implementations of the service-layer protocols."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.services.notifier import LinkButton

log = logging.getLogger("tgpanel.bot")


class AiogramSender:
    """``MessageSender`` over a Bot that may be bound later (the pipeline is built first)."""

    def __init__(self, bot: Bot | None = None) -> None:
        self._bot = bot

    def bind(self, bot: Bot | None) -> None:
        self._bot = bot

    async def send_message(
        self, chat_id: int, text: str, *, button: LinkButton | None = None, html: bool = True
    ) -> None:
        if self._bot is None:
            raise RuntimeError("bot is not running")
        markup = None
        if button is not None:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=button.text, url=button.url)]]
            )
        await self._bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML if html else None,
            reply_markup=markup,
        )


class BotNotifier:
    """``Notifier``: plain-text message to every admin of the ``admins`` table."""

    def __init__(self, sender: AiogramSender, db: Database) -> None:
        self._sender = sender
        self._db = db

    async def notify_admins(self, text: str) -> None:
        try:
            admins = await self._db.run(repo.list_admins)
        except Exception:
            log.warning("cannot read admins")
            return
        for tg_id in admins:
            try:
                await self._sender.send_message(tg_id, text, html=False)
            except (TelegramAPIError, RuntimeError):
                log.warning("admin notification was not delivered")
            except Exception:
                log.warning("admin notification failed: %s", "unexpected error")
