"""User part of the bot (PLAN 6.1)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tgpanel.bot import keyboards, texts
from tgpanel.bot.delivery import send_link
from tgpanel.bot.deps import BotDeps
from tgpanel.bot.format import esc, fmt_dt
from tgpanel.db import repo
from tgpanel.domain.models import UserRecord, UserStatus
from tgpanel.services.requests import RequestKind, RequestOutcome

log = logging.getLogger("tgpanel.bot")
router = Router(name="user")


async def _status_text(deps: BotDeps, user: UserRecord) -> str:
    if user.status is UserStatus.DISABLED:
        return texts.STATUS_DISABLED_USER
    if user.status is UserStatus.EXPIRED:
        return texts.STATUS_EXPIRED_USER
    tz = (await deps.settings.snapshot()).timezone
    expires = None if user.expires_at is None else fmt_dt(user.expires_at, tz)
    return texts.status_active(expires, imported=user.imported)


async def notify_admins_of_request(bot: Bot, deps: BotDeps, req: repo.AccessRequest) -> None:
    tz = (await deps.settings.snapshot()).timezone
    text = texts.request_card(
        esc(req.full_name), req.tg_username, req.tg_id, fmt_dt(req.created_at, tz)
    )
    for admin in await deps.db.run(repo.list_admins):
        try:
            await bot.send_message(admin, text, reply_markup=keyboards.request_actions(req.id))
        except Exception:
            log.warning("request notification was not delivered")


@router.message(CommandStart())
async def cmd_start(message: Message, deps: BotDeps, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    uid = message.from_user.id
    info = await deps.requests.register_start(
        uid, message.from_user.username, message.from_user.full_name
    )
    is_admin = await deps.db.run(repo.is_admin, uid)
    suffix = texts.ADMIN_HINT if is_admin else ""
    if info.user is not None:
        active = info.user.status is UserStatus.ACTIVE
        await message.answer(
            await _status_text(deps, info.user) + suffix,
            reply_markup=keyboards.user_start(can_request=False, has_link=active),
        )
        if info.first_start and active and message.bot is not None:
            await send_link(message.bot, deps, uid, info.user, texts.LINK_FROM_ADMIN)
        return
    pending = await deps.db.run(repo.pending_request_for, uid)
    if pending is not None and await deps.requests.issuance_mode() != "open":
        await message.answer(texts.REQUEST_PENDING + suffix)
        return
    open_mode = await deps.requests.issuance_mode() == "open"
    default = texts.START_NO_PROFILE_OPEN if open_mode else texts.DEFAULT_WELCOME
    welcome = await deps.templates.render(
        "msg.welcome", default, {"name": message.from_user.full_name}
    )
    await message.answer(
        welcome + suffix,
        reply_markup=keyboards.user_start(can_request=True, has_link=False),
    )


@router.callback_query(F.data == "my")
async def cb_my_link(cb: CallbackQuery, deps: BotDeps, bot: Bot) -> None:
    user = await deps.db.run(repo.get_user_by_tg_id, cb.from_user.id)
    if user is None:
        await cb.answer(texts.NO_ACCESS, show_alert=True)
        return
    user = await deps.requests.adopt_telegram_name(
        user, cb.from_user.username, cb.from_user.full_name
    )
    if user.status is not UserStatus.ACTIVE:
        await cb.answer(await _status_text(deps, user), show_alert=True)
        return
    await cb.answer()
    if not await send_link(bot, deps, cb.from_user.id, user, texts.ACCESS_READY):
        await bot.send_message(cb.from_user.id, texts.LINK_UNAVAILABLE)


async def deliver_outcome(
    bot: Bot,
    deps: BotDeps,
    tg_id: int,
    out: RequestOutcome,
    intro: str = "",
    *,
    key: str = "msg.link",
    default: str = texts.DEFAULT_LINK,
) -> None:
    """Send the freshly issued link to the requester (only if the apply succeeded)."""
    if out.user is None or out.link is None:
        await bot.send_message(tg_id, texts.LINK_UNAVAILABLE)
        return
    await send_link(bot, deps, tg_id, out.user, intro, key=key, default=default)


@router.callback_query(F.data == "req")
async def cb_request(cb: CallbackQuery, deps: BotDeps, bot: Bot) -> None:
    tg_id = cb.from_user.id
    chat = cb.from_user.id

    async def preparing() -> None:
        await bot.send_message(chat, texts.PREPARING)

    await cb.answer()
    out = await deps.requests.submit(
        tg_id, cb.from_user.username, cb.from_user.full_name, on_preparing=preparing
    )
    kind = out.kind
    if kind is RequestKind.CREATED and out.request is not None:
        await bot.send_message(chat, texts.REQUEST_SENT)
        await notify_admins_of_request(bot, deps, out.request)
    elif kind is RequestKind.ALREADY_PENDING:
        await bot.send_message(chat, texts.REQUEST_PENDING)
    elif kind is RequestKind.RATE_LIMITED:
        await bot.send_message(chat, texts.REQUEST_RATE_LIMITED)
    elif kind is RequestKind.BLACKLISTED:
        await bot.send_message(chat, texts.REQUEST_DENIED)
    elif kind is RequestKind.HAS_ACCESS and out.user is not None:
        await bot.send_message(
            chat,
            await _status_text(deps, out.user),
            reply_markup=keyboards.user_start(can_request=False, has_link=True),
        )
    elif kind is RequestKind.ISSUED:
        await deliver_outcome(bot, deps, chat, out, texts.ACCESS_READY)
    else:  # FAILED and anything unexpected: no link
        await bot.send_message(chat, texts.PREPARE_FAILED)


@router.callback_query()
async def cb_fallback(cb: CallbackQuery) -> None:
    """Unknown or not permitted callbacks (e.g. admin callbacks from a non-admin)."""
    await cb.answer(texts.UNKNOWN_ACTION, show_alert=True)
