"""Admin part of the bot (PLAN 6.2). Every handler sits behind ``AdminFilter``."""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from tgpanel.apply.errors import OperationRejected
from tgpanel.bot import icons, keyboards, texts
from tgpanel.bot.delivery import alert, send_link, show
from tgpanel.bot.deps import BotDeps, totals_pair
from tgpanel.bot.format import esc, fmt_dt, human_bytes, status_mark, status_ru
from tgpanel.bot.user import deliver_outcome
from tgpanel.db import repo
from tgpanel.domain.expiry import Term, default_expiry
from tgpanel.domain.models import UserRecord, UserStatus, clean_display_name, shown_name
from tgpanel.domain.queries import Period, SortField, UserFilter, UserListQuery
from tgpanel.services.api import NewUser
from tgpanel.services.broadcast import BroadcastPreview
from tgpanel.services.errors import UserServiceError
from tgpanel.services.requests import RequestKind

log = logging.getLogger("tgpanel.bot")

SERVICE_PAGE = 50
MAX_TG_ID = 2**53
SORTS: dict[str, tuple[SortField, bool]] = {
    "n": ("display_name", False),
    "l": ("last_seen_at", True),
    "t": ("traffic", True),
    "x": ("expires_at", False),
}
STATUS_FILTERS = {
    "a": (),
    "A": (UserStatus.ACTIVE,),
    "d": (UserStatus.DISABLED,),
    "e": (UserStatus.EXPIRED,),
}


class AdminFilter(Filter):
    async def __call__(self, event: TelegramObject, deps: BotDeps) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        return bool(await deps.db.run(repo.is_admin, user.id))


class CreateUser(StatesGroup):
    name = State()
    display_name = State()
    tg_id = State()
    comment = State()


class EditComment(StatesGroup):
    text = State()


class EditName(StatesGroup):
    text = State()


class Search(StatesGroup):
    query = State()


class BroadcastText(StatesGroup):
    text = State()


router = Router(name="admin")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


def actor_of(user_id: int) -> str:
    return f"bot:{user_id}"


def uid_cb(prefix: str) -> Any:
    return F.data.regexp(rf"^{prefix}:(\d{{1,9}})$").as_("m")


def _uid(m: re.Match[str]) -> int:
    return int(m.group(1))


# ------------------------------------------------------------------ menu


async def _menu(deps: BotDeps) -> tuple[str, InlineKeyboardMarkup]:
    pending = len(await deps.requests.list_pending())
    return texts.ADMIN_MENU, keyboards.admin_menu(pending)


@router.message(Command("admin"))
async def cmd_admin(message: Message, deps: BotDeps, state: FSMContext) -> None:
    await state.set_state(None)
    text, markup = await _menu(deps)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.set_state(None)
    await message.answer(texts.CANCELLED)


@router.callback_query(F.data == "m")
async def cb_menu(cb: CallbackQuery, deps: BotDeps, state: FSMContext) -> None:
    await state.set_state(None)
    text, markup = await _menu(deps)
    await show(cb, text, markup)


# ------------------------------------------------------------------ user list


async def _selected(state: FSMContext) -> set[int]:
    return {int(i) for i in (await state.get_data()).get("sel", [])}


async def _render_list(
    deps: BotDeps, state: FSMContext, page: int, flt: str, sort: str, search: bool
) -> tuple[str, InlineKeyboardMarkup]:
    query = str((await state.get_data()).get("q", "")) if search else ""
    field, desc = SORTS[sort]
    per = keyboards.PAGE_SIZE

    async def fetch(p: int) -> Any:
        return await deps.users.list(
            UserListQuery(
                filter=UserFilter(query=query or None, statuses=STATUS_FILTERS[flt]),
                sort=field,
                descending=desc,
                page=p // (SERVICE_PAGE // per) + 1,
                per_page=50,
            )
        )

    result = await fetch(max(page, 0))
    pages = max(1, math.ceil(result.total / per))
    if page >= pages:
        page = pages - 1
        result = await fetch(page)
    offset = (page % (SERVICE_PAGE // per)) * per
    rows = result.rows[offset : offset + per]
    sel = await _selected(state)
    marks = {
        r.user.id: status_mark(r.user.status) + (icons.SELECTED if r.user.id in sel else "")
        for r in rows
    }
    head = f"{icons.USERS} Пользователи: {result.total}, страница {page + 1} из {pages}"
    if search and query:
        head += f"\n{icons.SEARCH} Поиск: «{esc(query)}»"
    if not rows:
        head += "\n" + texts.LIST_EMPTY
    markup = keyboards.user_list(
        rows, page=page, pages=pages, flt=flt, sort=sort, search=search, marks=marks
    )
    return head, markup


@router.callback_query(F.data.regexp(r"^l:(\d{1,4}):([aAde]):([nltx]):([01])$").as_("m"))
async def cb_list(cb: CallbackQuery, deps: BotDeps, state: FSMContext, m: re.Match[str]) -> None:
    await state.set_state(None)
    text, markup = await _render_list(
        deps, state, int(m.group(1)), m.group(2), m.group(3), m.group(4) == "1"
    )
    await show(cb, text, markup)


@router.callback_query(F.data == "se")
async def cb_search(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Search.query)
    await show(cb, texts.SEARCH_PROMPT)


@router.message(Search.query, F.text, ~F.text.startswith("/"))
async def msg_search(message: Message, deps: BotDeps, state: FSMContext) -> None:
    query = (message.text or "").strip()[:100]
    await state.set_state(None)
    await state.update_data(q=query)
    text, markup = await _render_list(deps, state, 0, "a", "n", bool(query))
    await message.answer(text, reply_markup=markup)


# ------------------------------------------------------------------ user card


async def _traffic_line(deps: BotDeps, user_id: int) -> str:
    parts: list[str] = []
    for label, period in (("24 ч", "24h"), ("7 дн", "7d"), ("30 дн", "30d")):
        p: Period = period  # type: ignore[assignment]
        try:
            up, down = totals_pair(await deps.traffic.user_totals(user_id, p))
            traffic = f"{icons.UP}{human_bytes(up)} {icons.DOWN}{human_bytes(down)}"
            parts.append(f"{icons.APPLY} {label}: {traffic}")
        except Exception:
            parts.append(f"{icons.APPLY} {label}: нет данных")
    return "\n".join(parts)


async def _card(
    deps: BotDeps, state: FSMContext, uid: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await deps.users.get(uid)
    extra = await deps.db.run(repo.get_user_extra, uid)
    if user is None or extra is None:
        return None
    tz = (await deps.settings.snapshot()).timezone
    tg = "—"
    if user.tg_id is not None:
        tg = str(user.tg_id) + (f" (@{esc(extra.tg_username)})" if extra.tg_username else "")
    bot_state = "запущен" if extra.bot_started else "не запущен"
    if extra.bot_started and not extra.can_message:
        bot_state = "заблокирован"
    lines = [
        f"{icons.USER} <b>{esc(shown_name(user))}</b> (#{user.id})",
        *([f"{icons.USER} Профиль: <code>{esc(user.name)}</code>"] if user.display_name else []),
        f"{status_mark(user.status)} Статус: {status_ru(user.status)}",
        f"{icons.TERM} Срок: {fmt_dt(user.expires_at, tz, 'без срока')}",
        f"{icons.COMMENT} Комментарий: {esc(user.comment) if user.comment else '—'}",
        f"{icons.TELEGRAM} Telegram: {tg}",
        f"{icons.BOT} Бот: {bot_state}",
        f"{icons.DATE} Последняя активность: {fmt_dt(extra.last_seen_at, tz)}",
        await _traffic_line(deps, uid),
    ]
    selected = uid in await _selected(state)
    return "\n".join(lines), keyboards.user_card(
        uid, enabled=user.status is UserStatus.ACTIVE, selected=selected
    )


async def _show_card(
    target: CallbackQuery | Message, deps: BotDeps, state: FSMContext, uid: int, prefix: str = ""
) -> None:
    card = await _card(deps, state, uid)
    if card is None:
        await show(target, texts.USER_NOT_FOUND, keyboards.kb(keyboards.menu_row()))
        return
    text, markup = card
    await show(target, (prefix + "\n\n" if prefix else "") + text, markup)


@router.callback_query(uid_cb("c"))
async def cb_card(cb: CallbackQuery, deps: BotDeps, state: FSMContext, m: re.Match[str]) -> None:
    await state.set_state(None)
    await _show_card(cb, deps, state, _uid(m))


@router.callback_query(F.data.regexp(r"^t:(\d{1,9}):([01])$").as_("m"))
async def cb_toggle(cb: CallbackQuery, deps: BotDeps, state: FSMContext, m: re.Match[str]) -> None:
    """Sets the target state carried by the button, so a double click changes nothing more."""
    uid, enable = int(m.group(1)), m.group(2) == "1"
    user = await deps.users.get(uid)
    if user is None:
        await alert(cb, texts.USER_NOT_FOUND)
        return
    await cb.answer(texts.OPERATION_RUNNING)
    if (user.status is UserStatus.ACTIVE) == enable:
        await _show_card(cb, deps, state, uid)
        return
    res = await deps.users.set_status([uid], enable, actor_of(cb.from_user.id))
    await _show_card(cb, deps, state, uid, esc(res.error) if not res.ok and res.error else "")


@router.callback_query(uid_cb("x"))
async def cb_extend_menu(cb: CallbackQuery, m: re.Match[str]) -> None:
    await show(cb, texts.BTN_EXTEND + ":", keyboards.extend_menu(_uid(m)))


@router.callback_query(F.data.regexp(r"^xe:(\d{1,9}):(7|30|90|365)$").as_("m"))
async def cb_extend(cb: CallbackQuery, deps: BotDeps, state: FSMContext, m: re.Match[str]) -> None:
    uid = int(m.group(1))
    await cb.answer(texts.OPERATION_RUNNING)
    res = await deps.users.extend([uid], int(m.group(2)), actor_of(cb.from_user.id))
    await _show_card(
        cb, deps, state, uid, esc(res.error) if not res.ok and res.error else texts.DONE
    )


@router.callback_query(uid_cb("d"))
async def cb_delete_ask(cb: CallbackQuery, deps: BotDeps, m: re.Match[str]) -> None:
    uid = _uid(m)
    user = await deps.users.get(uid)
    if user is None:
        await alert(cb, texts.USER_NOT_FOUND)
        return
    await show(
        cb, texts.CONFIRM_DELETE.format(name=esc(shown_name(user))), keyboards.confirm_delete(uid)
    )


@router.callback_query(uid_cb("dy"))
async def cb_delete(cb: CallbackQuery, deps: BotDeps, m: re.Match[str]) -> None:
    await cb.answer(texts.OPERATION_RUNNING)
    res = await deps.users.delete([_uid(m)], actor_of(cb.from_user.id))
    msg = texts.DELETED if res.ok else esc(res.error or texts.ERROR_GENERIC)
    await show(cb, msg, keyboards.kb(keyboards.menu_row()))


async def _user_or_alert(cb: CallbackQuery, deps: BotDeps, uid: int) -> UserRecord | None:
    user = await deps.users.get(uid)
    if user is None:
        await alert(cb, texts.USER_NOT_FOUND)
    return user


@router.callback_query(uid_cb("k"))
async def cb_link(cb: CallbackQuery, deps: BotDeps, bot: Bot, m: re.Match[str]) -> None:
    user = await _user_or_alert(cb, deps, _uid(m))
    if user is None:
        return
    await cb.answer()
    intro = texts.LINK_OF.format(name=shown_name(user))
    if not await send_link(bot, deps, cb.from_user.id, user, intro, notify_blocked=False):
        await bot.send_message(cb.from_user.id, texts.LINK_UNAVAILABLE)


@router.callback_query(uid_cb("q"))
async def cb_qr(cb: CallbackQuery, deps: BotDeps, bot: Bot, m: re.Match[str]) -> None:
    await cb_link(cb, deps, bot, m)


@router.callback_query(uid_cb("s"))
async def cb_send_link(cb: CallbackQuery, deps: BotDeps, bot: Bot, m: re.Match[str]) -> None:
    user = await _user_or_alert(cb, deps, _uid(m))
    if user is None:
        return
    extra = await deps.db.run(repo.get_user_extra, user.id)
    reason = None
    if user.tg_id is None:
        reason = texts.REASON_NO_TG
    elif extra is None or not extra.bot_started:
        reason = texts.REASON_NOT_STARTED
    elif not extra.can_message:
        reason = texts.REASON_BLOCKED
    elif user.status is not UserStatus.ACTIVE:
        reason = status_ru(user.status)
    if reason is not None or user.tg_id is None:
        await alert(cb, texts.CANNOT_SEND.format(reason=reason))
        return
    ok = await send_link(bot, deps, user.tg_id, user, texts.LINK_FROM_ADMIN)
    await alert(
        cb, texts.SENT_TO_USER if ok else texts.CANNOT_SEND.format(reason=texts.REASON_BLOCKED)
    )


@router.callback_query(uid_cb("sel"))
async def cb_select(cb: CallbackQuery, deps: BotDeps, state: FSMContext, m: re.Match[str]) -> None:
    uid = _uid(m)
    sel = await _selected(state)
    sel ^= {uid}
    await state.update_data(sel=sorted(sel))
    await _show_card(cb, deps, state, uid)


# ------------------------------------------------------------------ comment


@router.callback_query(uid_cb("ce"))
async def cb_comment_edit(cb: CallbackQuery, state: FSMContext, m: re.Match[str]) -> None:
    await state.set_state(EditComment.text)
    await state.update_data(edit_uid=_uid(m))
    await show(cb, texts.COMMENT_PROMPT)


@router.message(EditComment.text, F.text, ~F.text.startswith("/"))
async def msg_comment(message: Message, deps: BotDeps, state: FSMContext) -> None:
    data = await state.get_data()
    uid = int(data.get("edit_uid", 0))
    await state.set_state(None)
    try:
        await deps.users.update_meta(
            uid,
            actor_of(message.from_user.id if message.from_user else 0),
            comment=message.text or "",
        )
    except (UserServiceError, OperationRejected) as exc:
        await message.answer(esc(str(exc)))
        return
    await _show_card(message, deps, state, uid, texts.COMMENT_SAVED)


@router.callback_query(uid_cb("en"))
async def cb_name_edit(cb: CallbackQuery, state: FSMContext, m: re.Match[str]) -> None:
    await state.set_state(EditName.text)
    await state.update_data(edit_uid=_uid(m))
    await show(cb, texts.NAME_PROMPT)


@router.message(EditName.text, F.text, ~F.text.startswith("/"))
async def msg_display_name(message: Message, deps: BotDeps, state: FSMContext) -> None:
    data = await state.get_data()
    uid = int(data.get("edit_uid", 0))
    raw = (message.text or "").strip()
    try:
        await deps.users.update_meta(
            uid,
            actor_of(message.from_user.id if message.from_user else 0),
            display_name="" if raw == "-" else raw,
        )
    except (UserServiceError, OperationRejected) as exc:
        await message.answer(esc(str(exc)))  # stay in the state: the admin can retry
        return
    await state.set_state(None)
    await _show_card(message, deps, state, uid, texts.NAME_SAVED)


# ------------------------------------------------------------------ requests


@router.callback_query(F.data == "rq")
async def cb_requests(cb: CallbackQuery, deps: BotDeps, bot: Bot) -> None:
    pending = await deps.requests.list_pending()
    await cb.answer()
    if not pending:
        await show(cb, texts.NO_REQUESTS, keyboards.kb(keyboards.menu_row()))
        return
    await show(cb, f"{icons.REQUESTS} Заявок: {len(pending)}", keyboards.kb(keyboards.menu_row()))
    tz = (await deps.settings.snapshot()).timezone
    for req in pending[:20]:
        text = texts.request_card(
            esc(req.full_name), req.tg_username, req.tg_id, fmt_dt(req.created_at, tz)
        )
        await bot.send_message(
            cb.from_user.id, text, reply_markup=keyboards.request_actions(req.id)
        )


@router.callback_query(uid_cb("ra"))
async def cb_request_approve(cb: CallbackQuery, deps: BotDeps, m: re.Match[str]) -> None:
    rid = _uid(m)
    req = await deps.requests.get(rid)
    if req is None:
        await alert(cb, texts.REQUEST_NOT_FOUND)
        return
    if req.status != "pending":
        await alert(cb, texts.REQUEST_DECIDED.format(status=texts.REQUEST_STATUS_RU[req.status]))
        return
    await show(
        cb, texts.CHOOSE_TERM.format(name=esc(req.full_name)), keyboards.term_choice("rt", rid)
    )


@router.callback_query(F.data.regexp(r"^rt:(\d{1,9}):(1d|1m|1y|df)$").as_("m"))
async def cb_request_term(cb: CallbackQuery, deps: BotDeps, bot: Bot, m: re.Match[str]) -> None:
    rid, code = int(m.group(1)), m.group(2)
    term = None if code == "df" else Term(code)
    await cb.answer(texts.OPERATION_RUNNING)
    out = await deps.requests.approve(rid, term, actor_of(cb.from_user.id))
    if out.kind is RequestKind.ISSUED and out.request is not None:
        name = shown_name(out.user) if out.user else out.request.full_name
        await show(cb, texts.CREATED.format(name=esc(name)), keyboards.kb(keyboards.menu_row()))
        await deliver_outcome(
            bot,
            deps,
            out.request.tg_id,
            out,
            key="msg.approved",
            default=texts.DEFAULT_APPROVED,
        )
    elif out.kind is RequestKind.ALREADY_DECIDED and out.request is not None:
        await show(
            cb,
            texts.REQUEST_DECIDED.format(status=texts.REQUEST_STATUS_RU[out.request.status]),
            keyboards.kb(keyboards.menu_row()),
        )
    elif out.kind is RequestKind.NOT_FOUND:
        await show(cb, texts.REQUEST_NOT_FOUND, keyboards.kb(keyboards.menu_row()))
    else:
        await show(
            cb,
            esc(out.error or texts.ERROR_GENERIC),
            keyboards.request_actions(rid),
        )


@router.callback_query(uid_cb("rr"))
async def cb_request_reject(cb: CallbackQuery, deps: BotDeps, bot: Bot, m: re.Match[str]) -> None:
    out = await deps.requests.reject(_uid(m), actor_of(cb.from_user.id))
    if out.kind is RequestKind.REJECTED and out.request is not None:
        await show(cb, texts.REQUEST_REJECTED_ADMIN, keyboards.kb(keyboards.menu_row()))
        notice = await deps.templates.render(
            "msg.rejected", texts.DEFAULT_REJECTED, {"name": out.request.full_name}
        )
        try:
            await bot.send_message(out.request.tg_id, notice)
        except TelegramForbiddenError:
            log.info("rejection notice was not delivered: bot blocked")
        return
    if out.kind is RequestKind.ALREADY_DECIDED and out.request is not None:
        await alert(
            cb, texts.REQUEST_DECIDED.format(status=texts.REQUEST_STATUS_RU[out.request.status])
        )
        return
    await alert(cb, texts.REQUEST_NOT_FOUND)


# ------------------------------------------------------------------ mode / apply / backup


@router.callback_query(F.data == "md")
async def cb_mode(cb: CallbackQuery, deps: BotDeps) -> None:
    mode = await deps.requests.issuance_mode()
    await show(cb, texts.MODE_TEXT.format(mode=texts.mode_label(mode)), keyboards.mode_menu(mode))


@router.callback_query(F.data.regexp(r"^ms:(open|approval)$").as_("m"))
async def cb_mode_set(cb: CallbackQuery, deps: BotDeps, m: re.Match[str]) -> None:
    res = await deps.settings.set("issuance_mode", m.group(1), actor_of(cb.from_user.id))
    mode = await deps.requests.issuance_mode()
    prefix = "" if res.ok else esc(res.error or texts.ERROR_GENERIC) + "\n"
    await show(
        cb,
        prefix + texts.MODE_TEXT.format(mode=texts.mode_label(mode)),
        keyboards.mode_menu(mode),
    )


@router.callback_query(F.data == "ap")
async def cb_apply_status(cb: CallbackQuery, deps: BotDeps) -> None:
    runs = await deps.db.run(repo.list_apply_runs, 5)
    tz = (await deps.settings.snapshot()).timezone
    lines = [
        f"{icons.APPLY} Применяется сейчас."
        if deps.pipeline.is_applying
        else f"{icons.APPLY} Сейчас ничего не применяется."
    ]
    for run in runs:
        line = f"#{run.id} {esc(run.status)}: {esc(run.reason)}, {fmt_dt(run.started_at, tz)}"
        if run.error:
            line += f"\n  {esc(run.error[:200])}"
        lines.append(line)
    if not runs:
        lines.append("Запусков ещё не было.")
    await show(cb, "\n".join(lines), keyboards.kb(keyboards.menu_row()))


@router.callback_query(F.data == "bk")
async def cb_backup(cb: CallbackQuery, deps: BotDeps) -> None:
    await cb.answer(texts.BACKUP_RUNNING)
    try:
        info = await deps.pipeline.create_backup("manual", actor_of(cb.from_user.id))
    except Exception as exc:
        text = (
            esc(str(exc))
            if isinstance(exc, OperationRejected | RuntimeError)
            else texts.ERROR_GENERIC
        )
        await show(cb, f"{icons.WARN} Бэкап не создан: " + text, keyboards.kb(keyboards.menu_row()))
        return
    await show(
        cb,
        f"{icons.OK} Бэкап создан, {human_bytes(info.size)}.",
        keyboards.kb(keyboards.menu_row()),
    )


# ------------------------------------------------------------------ create user dialog


@router.callback_query(F.data == "cr")
async def cb_create(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateUser.name)
    await state.update_data(cu={})
    await show(cb, texts.CREATE_NAME)


@router.message(CreateUser.name, F.text, ~F.text.startswith("/"))
async def msg_create_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100 or "\n" in name:
        await message.answer(texts.CREATE_BAD_TEXT)
        return
    await state.update_data(cu={"name": name})
    await state.set_state(CreateUser.display_name)
    await message.answer(texts.CREATE_DISPLAY_NAME, reply_markup=keyboards.skip_button("cs:dn"))


@router.message(CreateUser.display_name, F.text, ~F.text.startswith("/"))
async def msg_create_display_name(message: Message, state: FSMContext) -> None:
    try:
        value = clean_display_name(message.text or "")
    except ValueError:
        await message.answer(texts.CREATE_BAD_TEXT)
        return
    data = (await state.get_data())["cu"]
    data["display_name"] = value
    await state.update_data(cu=data)
    await state.set_state(CreateUser.tg_id)
    await message.answer(texts.CREATE_TG_ID, reply_markup=keyboards.skip_button("cs:tg"))


@router.callback_query(F.data == "cs:dn", CreateUser.display_name)
async def cb_create_skip_display_name(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateUser.tg_id)
    await show(cb, texts.CREATE_TG_ID, keyboards.skip_button("cs:tg"))


async def _ask_term(target: Message | CallbackQuery) -> None:
    await show(target, texts.CREATE_TERM, keyboards.term_choice("ct"))


@router.message(CreateUser.tg_id, F.text, ~F.text.startswith("/"))
async def msg_create_tg(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) <= MAX_TG_ID:
        await message.answer(texts.CREATE_BAD_TG_ID)
        return
    data = (await state.get_data())["cu"]
    data["tg_id"] = int(raw)
    await state.update_data(cu=data)
    await _ask_term(message)


@router.callback_query(F.data == "cs:tg", CreateUser.tg_id)
async def cb_create_skip_tg(cb: CallbackQuery) -> None:
    await _ask_term(cb)


@router.callback_query(F.data.regexp(r"^ct:(1d|1m|1y|df)$").as_("m"), CreateUser.tg_id)
async def cb_create_term(cb: CallbackQuery, state: FSMContext, m: re.Match[str]) -> None:
    data = (await state.get_data())["cu"]
    data["term"] = m.group(1)
    await state.update_data(cu=data)
    await state.set_state(CreateUser.comment)
    await show(cb, texts.CREATE_COMMENT, keyboards.skip_button("cs:cm"))


@router.message(CreateUser.comment, F.text, ~F.text.startswith("/"))
async def msg_create_comment(message: Message, deps: BotDeps, state: FSMContext, bot: Bot) -> None:
    await _finish_create(message, deps, state, bot, message.text or "")


@router.callback_query(F.data == "cs:cm", CreateUser.comment)
async def cb_create_skip_comment(
    cb: CallbackQuery, deps: BotDeps, state: FSMContext, bot: Bot
) -> None:
    await _finish_create(cb, deps, state, bot, "")


async def _finish_create(
    target: Message | CallbackQuery, deps: BotDeps, state: FSMContext, bot: Bot, comment: str
) -> None:
    data = (await state.get_data()).get("cu") or {}
    await state.set_state(None)
    await state.update_data(cu={})
    admin = target.from_user.id if target.from_user else 0
    if "name" not in data:
        await show(target, texts.ERROR_GENERIC)
        return
    term_code = data.get("term", "df")
    expires = None if term_code == "df" else default_expiry(Term(term_code), deps.pipeline.now())
    await show(target, texts.CREATING)
    res = await deps.users.create(
        [
            NewUser(
                name=data["name"],
                tg_id=data.get("tg_id"),
                comment=comment,
                expires_at=expires,
                display_name=data.get("display_name", ""),
            )
        ],
        actor_of(admin),
    )
    if not res.ok or not res.user_ids:
        await bot.send_message(admin, esc(res.error or texts.ERROR_GENERIC))
        return
    user = await deps.users.get(res.user_ids[0])
    await bot.send_message(
        admin,
        texts.CREATED.format(name=esc(data.get("display_name") or data["name"])),
        reply_markup=keyboards.kb(keyboards.menu_row()),
    )
    if user is not None:
        await send_link(
            bot,
            deps,
            admin,
            user,
            texts.LINK_OF.format(name=shown_name(user)),
            notify_blocked=False,
        )


# ------------------------------------------------------------------ broadcast


@router.callback_query(F.data == "bc")
async def cb_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await show(cb, texts.MENU_BROADCAST, keyboards.broadcast_menu(len(await _selected(state))))


@router.callback_query(F.data == "bcx")
async def cb_broadcast_clear(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sel=[])
    await show(cb, texts.SELECTION_CLEARED, keyboards.broadcast_menu(0))


@router.callback_query(F.data.in_({"bca", "bcs"}))
async def cb_broadcast_scope(cb: CallbackQuery, state: FSMContext) -> None:
    sel = sorted(await _selected(state))
    if cb.data == "bcs" and not sel:
        await alert(cb, texts.BROADCAST_NO_SELECTION)
        return
    await state.update_data(bc={"ids": sel if cb.data == "bcs" else None})
    await state.set_state(BroadcastText.text)
    await show(cb, texts.BROADCAST_ASK)


def _preview_text(preview: BroadcastPreview) -> str:
    lines = [
        f"{icons.BROADCAST} Получат: {len(preview.included)}. Исключены: {len(preview.excluded)}."
    ]
    for r in preview.excluded[:15]:
        lines.append(f"{icons.WARN} {esc(r.name)}: {r.reason_text}")
    if len(preview.excluded) > 15:
        lines.append(f"…и ещё {len(preview.excluded) - 15}")
    return "\n".join(lines)


@router.message(BroadcastText.text, F.text, ~F.text.startswith("/"))
async def msg_broadcast_text(message: Message, deps: BotDeps, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    template = (
        await deps.templates.custom("msg.broadcast") or texts.DEFAULT_BROADCAST
        if raw == "-"
        else raw
    )
    draft = (await state.get_data()).get("bc") or {}
    await state.set_state(None)
    preview = await deps.broadcast.preview(draft.get("ids"))
    if not preview.included:
        await state.update_data(bc=None)
        await message.answer(texts.BROADCAST_NO_ONE + "\n" + _preview_text(preview))
        return
    await state.update_data(bc={"ids": draft.get("ids"), "tpl": template})
    await message.answer(_preview_text(preview), reply_markup=keyboards.broadcast_confirm())


@router.callback_query(F.data == "bgo")
async def cb_broadcast_go(cb: CallbackQuery, deps: BotDeps, state: FSMContext, bot: Bot) -> None:
    draft = (await state.get_data()).get("bc")
    if not draft or "tpl" not in draft:
        await alert(cb, texts.BROADCAST_NO_DRAFT)
        return
    await state.update_data(bc=None)  # a second click finds no draft: no double send
    admin = cb.from_user.id
    ids, template = draft.get("ids"), str(draft["tpl"])
    await show(cb, f"{icons.BROADCAST} Рассылка запущена.", keyboards.kb(keyboards.menu_row()))

    async def job() -> None:
        try:
            rep = await deps.broadcast.start(template, actor_of(admin), ids)
            text = (
                f"{icons.OK} Рассылка #{rep.broadcast_id} завершена: отправлено {rep.sent}, "
                f"бот заблокирован {rep.forbidden}, ошибок {rep.errors}, "
                f"пропущено {rep.skipped}."
            )
            markup: InlineKeyboardMarkup | None = keyboards.report_button(rep.broadcast_id)
        except Exception as exc:
            log.warning("broadcast failed: %s", type(exc).__name__)
            text = esc(str(exc)) if isinstance(exc, OperationRejected) else texts.ERROR_GENERIC
            markup = None
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception as exc:
            log.warning("broadcast summary was not delivered: %s", type(exc).__name__)

    deps.spawn(job())


@router.callback_query(uid_cb("br"))
async def cb_broadcast_report(cb: CallbackQuery, deps: BotDeps, m: re.Match[str]) -> None:
    try:
        rep = await deps.broadcast.report(_uid(m))
    except OperationRejected as exc:
        await alert(cb, str(exc))
        return
    lines = [
        f"{icons.REPORT} Рассылка #{rep.broadcast_id}: всего {rep.total}, отправлено {rep.sent}."
    ]
    for item in rep.items[:60]:
        lines.append(f"• {esc(item.name or item.tg_id or '?')}: {item.result_text}")
    if rep.total > 60:
        lines.append(f"…и ещё {rep.total - 60}")
    await cb.answer()
    await show(cb, "\n".join(lines), keyboards.kb(keyboards.menu_row()))


# ------------------------------------------------------------------ blacklist


class BlacklistEdit(StatesGroup):
    add = State()
    remove = State()


async def _blacklist_view(deps: BotDeps) -> tuple[str, InlineKeyboardMarkup]:
    ids = await deps.requests.blacklist_ids()
    shown = ", ".join(str(i) for i in ids[:50]) or texts.BLACKLIST_EMPTY
    if len(ids) > 50:
        shown += f" …и ещё {len(ids) - 50}"
    return texts.BLACKLIST_TEXT.format(count=len(ids), ids=shown), keyboards.blacklist_menu()


@router.callback_query(F.data == "bl")
async def cb_blacklist(cb: CallbackQuery, deps: BotDeps, state: FSMContext) -> None:
    await state.set_state(None)
    text, markup = await _blacklist_view(deps)
    await show(cb, text, markup)


@router.callback_query(F.data.in_({"bla", "blr"}))
async def cb_blacklist_ask(cb: CallbackQuery, state: FSMContext) -> None:
    adding = cb.data == "bla"
    await state.set_state(BlacklistEdit.add if adding else BlacklistEdit.remove)
    await show(cb, texts.BLACKLIST_ASK_ADD if adding else texts.BLACKLIST_ASK_REMOVE)


async def _blacklist_apply(
    message: Message, deps: BotDeps, state: FSMContext, raw: str, add: bool
) -> None:
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) <= MAX_TG_ID:
        await message.answer(texts.BLACKLIST_BAD_ID)
        return
    await state.set_state(None)
    admin = message.from_user.id if message.from_user else 0
    try:
        await deps.requests.blacklist_edit(int(raw), add, actor_of(admin))
    except OperationRejected as exc:
        await message.answer(esc(str(exc)))
        return
    text, markup = await _blacklist_view(deps)
    await message.answer(text, reply_markup=markup)


@router.message(BlacklistEdit.add, F.text, ~F.text.startswith("/"))
async def msg_blacklist_add(message: Message, deps: BotDeps, state: FSMContext) -> None:
    await _blacklist_apply(message, deps, state, message.text or "", True)


@router.message(BlacklistEdit.remove, F.text, ~F.text.startswith("/"))
async def msg_blacklist_remove(message: Message, deps: BotDeps, state: FSMContext) -> None:
    await _blacklist_apply(message, deps, state, message.text or "", False)


@router.message(Command("ban", "unban"))
async def cmd_ban(message: Message, command: CommandObject, deps: BotDeps) -> None:
    await _blacklist_apply_cmd(message, command, deps)


async def _blacklist_apply_cmd(message: Message, command: CommandObject, deps: BotDeps) -> None:
    raw = (command.args or "").strip()
    if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) <= MAX_TG_ID:
        await message.answer(texts.BLACKLIST_USAGE)
        return
    admin = message.from_user.id if message.from_user else 0
    try:
        await deps.requests.blacklist_edit(int(raw), command.command == "ban", actor_of(admin))
    except OperationRejected as exc:
        await message.answer(esc(str(exc)))
        return
    text, markup = await _blacklist_view(deps)
    await message.answer(text, reply_markup=markup)
