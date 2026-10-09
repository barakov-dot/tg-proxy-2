"""Inline keyboards. Callback data is compact (<= 20 bytes) and validated by regexes."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tgpanel.bot import texts
from tgpanel.services.api import UserRow

PAGE_SIZE = 10  # divides the service page size (50)
FILTERS = "aAde"
SORTS = "nltx"


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def user_start(*, can_request: bool, has_link: bool) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if has_link:
        row.append(btn(texts.BTN_MY_LINK, "my"))
    if can_request:
        row.append(btn(texts.BTN_REQUEST, "req"))
    return kb(row)


def admin_menu(pending: int) -> InlineKeyboardMarkup:
    requests = texts.MENU_REQUESTS + (f" ({pending})" if pending else "")
    return kb(
        [btn(texts.MENU_USERS, "l:0:a:n:0"), btn(requests, "rq")],
        [btn(texts.MENU_CREATE, "cr"), btn(texts.MENU_BROADCAST, "bc")],
        [btn(texts.MENU_MODE, "md"), btn(texts.MENU_APPLY, "ap")],
        [btn(texts.MENU_BACKUP, "bk"), btn(texts.MENU_BLACKLIST, "bl")],
    )


def menu_row() -> list[InlineKeyboardButton]:
    return [btn(texts.BTN_MENU, "m")]


def list_state(page: int, flt: str, sort: str, search: bool) -> str:
    return f"l:{page}:{flt}:{sort}:{int(search)}"


def user_list(
    rows: tuple[UserRow, ...],
    *,
    page: int,
    pages: int,
    flt: str,
    sort: str,
    search: bool,
    marks: dict[int, str],
) -> InlineKeyboardMarkup:
    lines = [[btn(f"{marks[r.user.id]} {r.user.name[:34]}", f"c:{r.user.id}")] for r in rows]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn(texts.BTN_PREV, list_state(page - 1, flt, sort, search)))
    if page + 1 < pages:
        nav.append(btn(texts.BTN_NEXT, list_state(page + 1, flt, sort, search)))
    next_filter = FILTERS[(FILTERS.index(flt) + 1) % len(FILTERS)]
    next_sort = SORTS[(SORTS.index(sort) + 1) % len(SORTS)]
    controls = [
        btn(f"Фильтр: {texts.FILTER_NAMES[flt]}", list_state(0, next_filter, sort, search)),
        btn(f"Сортировка: {texts.SORT_NAMES[sort]}", list_state(0, flt, next_sort, search)),
    ]
    search_row = [btn(texts.BTN_SEARCH, "se")]
    if search:
        search_row.append(btn(texts.BTN_SEARCH_RESET, list_state(0, flt, sort, False)))
    return kb(*lines, nav, controls, search_row, menu_row())


def user_card(uid: int, *, enabled: bool, selected: bool) -> InlineKeyboardMarkup:
    return kb(
        [
            btn(
                texts.BTN_DISABLE if enabled else texts.BTN_ENABLE,
                f"t:{uid}:{0 if enabled else 1}",  # the state to set, not a toggle
            ),
            btn(texts.BTN_EXTEND, f"x:{uid}"),
        ],
        [btn(texts.BTN_LINK, f"k:{uid}"), btn(texts.BTN_QR, f"q:{uid}")],
        [btn(texts.BTN_SEND_LINK, f"s:{uid}"), btn(texts.BTN_COMMENT, f"ce:{uid}")],
        [btn(texts.BTN_UNSELECT if selected else texts.BTN_SELECT, f"sel:{uid}")],
        [btn(texts.BTN_DELETE, f"d:{uid}"), btn(texts.BTN_TO_LIST, "l:0:a:n:0")],
    )


def extend_menu(uid: int) -> InlineKeyboardMarkup:
    return kb(
        [btn(label, f"xe:{uid}:{days}") for days, label in texts.EXTEND_BUTTONS.items()],
        [btn(texts.BTN_BACK, f"c:{uid}")],
    )


def confirm_delete(uid: int) -> InlineKeyboardMarkup:
    return kb([btn(texts.BTN_DELETE_CONFIRM, f"dy:{uid}"), btn(texts.BTN_CANCEL, f"c:{uid}")])


def request_actions(rid: int) -> InlineKeyboardMarkup:
    return kb([btn(texts.BTN_APPROVE, f"ra:{rid}"), btn(texts.BTN_REJECT, f"rr:{rid}")])


def term_choice(prefix: str, ident: int | None = None) -> InlineKeyboardMarkup:
    head = f"{prefix}:{ident}:" if ident is not None else f"{prefix}:"
    return kb(
        [btn(label, head + code) for code, label in texts.TERM_BUTTONS.items()],
        [btn(texts.BTN_CANCEL, "m")],
    )


def skip_button(data: str) -> InlineKeyboardMarkup:
    return kb([btn(texts.BTN_SKIP, data)])


def mode_menu(mode: str) -> InlineKeyboardMarkup:
    other = "approval" if mode == "open" else "open"
    return kb(
        [btn(f"Переключить: {texts.mode_label(other)}", f"ms:{other}")],
        menu_row(),
    )


def broadcast_menu(selected: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("Всем", "bca"), btn(f"Выбранным ({selected})", "bcs")],
        [btn("Сбросить выбор", "bcx")],
        menu_row(),
    )


def broadcast_confirm() -> InlineKeyboardMarkup:
    return kb([btn(texts.BTN_START_BROADCAST, "bgo"), btn(texts.BTN_CANCEL, "m")])


def report_button(bid: int) -> InlineKeyboardMarkup:
    return kb([btn(texts.BTN_FULL_REPORT, f"br:{bid}")])


def blacklist_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn(texts.BLACKLIST_ADD, "bla"), btn(texts.BLACKLIST_REMOVE, "blr")],
        menu_row(),
    )
