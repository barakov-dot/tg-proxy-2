# ruff: noqa: RUF001
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from tests.bot.conftest import ADMIN, USER, Env
from tgpanel.bot import icons, keyboards, texts

MAX_LABEL = 24
EVIL = "<u>EVIL</u>&<script>"
BOT_DIR = Path(icons.__file__).parent


def _labels() -> list[str]:
    out = [
        v
        for k, v in vars(texts).items()
        if (k.startswith(("BTN_", "MENU_")) or k in ("BLACKLIST_ADD", "BLACKLIST_REMOVE"))
        and isinstance(v, str)
    ]
    out += [*texts.TERM_BUTTONS.values(), *texts.EXTEND_BUTTONS.values()]
    markups = [
        keyboards.user_start(can_request=True, has_link=True),
        keyboards.admin_menu(12),
        keyboards.user_card(5, enabled=True, selected=True),
        keyboards.user_card(5, enabled=False, selected=False),
        keyboards.extend_menu(5),
        keyboards.confirm_delete(5),
        keyboards.request_actions(5),
        keyboards.term_choice("ct"),
        keyboards.skip_button("x"),
        keyboards.mode_menu("open"),
        keyboards.mode_menu("approval"),
        keyboards.broadcast_menu(123),
        keyboards.broadcast_confirm(),
        keyboards.report_button(9),
        keyboards.blacklist_menu(),
        keyboards.kb(keyboards.menu_row()),
    ]
    for flt in keyboards.FILTERS:
        for sort in keyboards.SORTS:
            markups.append(
                keyboards.user_list((), page=1, pages=3, flt=flt, sort=sort, search=True, marks={})
            )
    for markup in markups:
        out += [b.text for row in markup.inline_keyboard for b in row]
    return out


def test_button_labels_fit_a_phone_and_are_russian() -> None:
    labels = _labels()
    assert len(labels) > 60
    for label in labels:
        assert len(label) <= MAX_LABEL, label
        assert re.search(r"[А-Яа-яЁё]|QR|ID", label), label


def test_emoji_are_defined_only_in_icons_module() -> None:
    for path in BOT_DIR.glob("*.py"):
        if path.name == "icons.py":
            continue
        for ch in path.read_text(encoding="utf-8"):
            assert unicodedata.category(ch) != "So" and ch != "️", (path.name, ch)
    names = [n for n in vars(icons) if n.isupper()]
    assert all(getattr(icons, n) for n in names)


def test_icons_show_in_main_screens() -> None:
    assert texts.BTN_MY_LINK.startswith(icons.LINK)
    assert texts.MENU_USERS.startswith(icons.USERS)
    assert keyboards.admin_menu(0).inline_keyboard[0][0].text.startswith(icons.USERS)
    assert texts.status_active(None).startswith(icons.ACTIVE)


async def test_status_icons_in_list_and_card(env: Env) -> None:
    active = await env.make_user("A", tg_id=USER, started=True)
    off = await env.make_user("B")
    await env.svc.users.set_status([off.id], False, "web:admin")
    await env.tg.press(ADMIN, "l:0:a:n:0")
    labels = [b.text for row in env.session.last_markup(ADMIN).inline_keyboard for b in row]  # type: ignore[union-attr]
    assert f"{icons.ACTIVE} A" in labels and f"{icons.DISABLED} B" in labels
    await env.tg.press(ADMIN, f"c:{active.id}")
    card = env.session.texts(ADMIN)[-1]
    assert card.startswith(f"{icons.USER} ") and f"{icons.ACTIVE} Статус: активен" in card
    assert icons.UP in card and icons.DOWN in card


async def test_no_raw_user_input_in_any_message(env: Env) -> None:
    """Hostile names/comments/usernames must reach Telegram only HTML-escaped."""
    env.set_setting("issuance_mode", "approval")
    user = await env.make_user(EVIL, tg_id=USER, started=True)
    await env.svc.users.update_meta(user.id, "web:admin", comment=EVIL, tg_username="evil")
    await env.make_user("B", tg_id=USER + 1, started=True)
    # a request from a user whose Telegram name is hostile
    await env.tg.press(USER + 5, "req", name=EVIL, username="evil_user")
    await env.tg.press(ADMIN, "rq")
    await env.tg.press(ADMIN, "ra:1")
    # list, search, card, extend menu, delete confirmation, send link, selection, blacklist
    await env.tg.press(ADMIN, "l:0:a:n:0")
    await env.tg.press(ADMIN, "se")
    await env.tg.send(ADMIN, EVIL)
    await env.tg.press(ADMIN, f"c:{user.id}")
    await env.tg.press(ADMIN, f"d:{user.id}")
    await env.tg.press(ADMIN, f"s:{user.id}")
    await env.tg.press(ADMIN, f"sel:{user.id}")
    await env.tg.press(ADMIN, f"ce:{user.id}")
    await env.tg.send(ADMIN, EVIL)
    # create dialog and broadcast with hostile text, preview and report
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, EVIL + " new")
    await env.tg.press(ADMIN, "cs:tg")
    await env.tg.press(ADMIN, "ct:df")
    await env.tg.send(ADMIN, EVIL)
    await env.tg.press(ADMIN, "bcs")
    await env.tg.send(ADMIN, "Привет " + EVIL + " {name}")
    await env.tg.press(ADMIN, "bgo")
    await env.deps.wait_background()
    await env.tg.press(ADMIN, "br:1")
    await env.tg.press(ADMIN, "ap")
    await env.tg.press(USER, "my")
    await env.tg.send(USER, "/start")
    everything = [
        *(t for chat in (ADMIN, USER, USER + 5) for t in env.session.texts(chat)),
        *(s.text for s in env.sender.sent),
        *(str(getattr(m, "caption", "") or "") for n, m in env.session.calls if n == "SendPhoto"),
    ]
    assert len(everything) > 15
    assert any("&lt;u&gt;EVIL&lt;/u&gt;&amp;&lt;script&gt;" in t for t in everything)
    for text in everything:
        assert "<u>EVIL" not in text and "<script" not in text, text
        assert "&<script" not in text
        # the only markup the bot itself emits is <b> (card title) and <code>
        assert not re.search(r"<(?!/?(?:b|code)>)[A-Za-z]", text), text
