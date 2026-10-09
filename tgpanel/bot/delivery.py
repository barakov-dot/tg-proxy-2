"""Sending links (text + button + QR) and common reply helpers."""

from __future__ import annotations

import io

import segno
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from tgpanel.bot import keyboards, texts
from tgpanel.bot.deps import BotDeps
from tgpanel.bot.format import esc
from tgpanel.domain.models import UserRecord
from tgpanel.services.errors import UserServiceError
from tgpanel.services.notifier import message_values, render_message


def qr_png(link: str) -> bytes:
    buf = io.BytesIO()
    segno.make(link, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


async def send_link(
    bot: Bot,
    deps: BotDeps,
    chat_id: int,
    user: UserRecord,
    intro: str = "",
    *,
    key: str = "msg.link",
    default: str = texts.DEFAULT_LINK,
    notify_blocked: bool = True,
) -> bool:
    """Link as text + connect button, then the QR picture. False if it could not be delivered.

    The text is the web-edited template ``key`` (or ``default``); ``{intro}`` is replaced by
    ``intro`` in the default link template.
    """
    try:
        link = deps.users.link(user)
        tg_link = deps.users.tg_link(user)
    except UserServiceError:
        return False
    cfg = await deps.settings.snapshot()
    values = message_values(user, link, tg_link, cfg.timezone, deps.pipeline.now())
    template = await deps.templates.custom(key) or default
    text = render_message(template, values).replace("{intro}", esc(intro))
    try:
        await bot.send_message(
            chat_id, text, reply_markup=keyboards.kb([keyboards.url_btn(texts.BTN_CONNECT, link)])
        )
        await bot.send_photo(
            chat_id, BufferedInputFile(qr_png(link), filename="qr.png"), caption=texts.QR_CAPTION
        )
    except TelegramForbiddenError:
        if notify_blocked and user.tg_id == chat_id:
            await deps.messenger.mark_blocked(chat_id)
        return False
    return True


async def show(
    target: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the message under a pressed button (or answer a text message) and ack the callback.

    An inaccessible (old) message or a failed edit falls back to a fresh message; the callback
    is always answered.
    """
    if isinstance(target, CallbackQuery):
        try:
            if isinstance(target.message, Message):
                try:
                    await target.message.edit_text(text, reply_markup=markup)
                except TelegramBadRequest:  # not modified / too old to edit
                    await target.message.answer(text, reply_markup=markup)
            elif target.bot is not None:
                await target.bot.send_message(target.from_user.id, text, reply_markup=markup)
        finally:
            await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def alert(cb: CallbackQuery, text: str) -> None:
    await cb.answer(text, show_alert=True)
