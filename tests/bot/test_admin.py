from __future__ import annotations

import html

from tests.bot.conftest import ADMIN, USER, Env
from tests.bot.helpers import forbidden
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus

OUTSIDER = 777


async def test_admin_menu(env: Env) -> None:
    await env.tg.send(ADMIN, "/admin")
    assert "l:0:a:n:0" in env.session.button_data(ADMIN)


async def test_non_admin_cannot_use_admin_command_or_callbacks(env: Env) -> None:
    user = await env.make_user("Victim", tg_id=USER, started=True)
    backups = len(env.svc.ctx.db.call(repo.list_backups))
    await env.tg.send(OUTSIDER, "/admin")
    assert env.session.texts(OUTSIDER) == []
    for data in (f"dy:{user.id}", f"t:{user.id}:0", "bk", "ms:open", "rt:1:1m", "bgo", "cr"):
        await env.tg.press(OUTSIDER, data)
    assert await env.svc.users.get(user.id) is not None
    assert (await env.svc.users.get(user.id)).status is UserStatus.ACTIVE  # type: ignore[union-attr]
    assert env.session.texts(OUTSIDER) == []
    assert env.svc.ctx.db.call(repo.get_setting, "issuance_mode") is None
    assert len(env.session.alerts()) == 7  # each one refused with an alert
    assert len(env.svc.ctx.db.call(repo.list_backups)) == backups


async def test_list_search_filter_sort_pagination(env: Env) -> None:
    for i in range(23):
        await env.make_user(f"user{i:02d}")
    await env.tg.press(ADMIN, "l:0:a:n:0")
    data = env.session.button_data(ADMIN)
    assert sum(d.startswith("c:") for d in data) == 10
    assert "l:1:a:n:0" in data
    env.session.clear()
    await env.tg.press(ADMIN, "l:2:a:n:0")
    assert sum(d.startswith("c:") for d in env.session.button_data(ADMIN)) == 3
    env.session.clear()
    await env.tg.press(ADMIN, "l:9:a:n:0")  # out of range: clamped to the last page
    assert "страница 3 из 3" in env.session.texts(ADMIN)[0]
    # search
    env.session.clear()
    await env.tg.press(ADMIN, "se")
    await env.tg.send(ADMIN, "user07")
    assert sum(d.startswith("c:") for d in env.session.button_data(ADMIN)) == 1
    # status filter: disable one and look at "disabled"
    await env.svc.users.set_status([1], False, "web:admin")
    env.session.clear()
    await env.tg.press(ADMIN, "l:0:d:n:0")
    assert env.session.button_data(ADMIN)[0] == "c:1"
    # sort by traffic works without errors
    await env.tg.press(ADMIN, "l:0:a:t:0")


async def test_user_card_traffic_comment_and_html_escape(env: Env) -> None:
    user = await env.make_user("<b>Evil</b> & Co", tg_id=USER, started=True)
    await env.tg.press(ADMIN, f"c:{user.id}")
    text = env.session.texts(ADMIN)[0]
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in text
    assert "<b>Evil</b>" not in text
    assert "24 ч" in text and "7 дн" in text and "30 дн" in text and "ГБ" in text
    # comment edit
    await env.tg.press(ADMIN, f"ce:{user.id}")
    await env.tg.send(ADMIN, "note <i>x</i>")
    assert (await env.svc.users.get(user.id)).comment == "note <i>x</i>"  # type: ignore[union-attr]
    assert "note &lt;i&gt;x&lt;/i&gt;" in env.session.texts(ADMIN)[-1]


async def test_toggle_extend_and_delete_with_confirmation(env: Env) -> None:
    user = await env.make_user("Vasya", tg_id=USER, started=True)
    await env.tg.press(ADMIN, f"t:{user.id}:0")
    assert (await env.svc.users.get(user.id)).status is UserStatus.DISABLED  # type: ignore[union-attr]
    await env.tg.press(ADMIN, f"t:{user.id}:1")
    assert (await env.svc.users.get(user.id)).status is UserStatus.ACTIVE  # type: ignore[union-attr]
    old = (await env.svc.users.get(user.id)).expires_at  # type: ignore[union-attr]
    await env.tg.press(ADMIN, f"x:{user.id}")
    await env.tg.press(ADMIN, f"xe:{user.id}:30")
    assert (await env.svc.users.get(user.id)).expires_at > old  # type: ignore[union-attr,operator]
    # delete: asking changes nothing, confirming deletes
    await env.tg.press(ADMIN, f"d:{user.id}")
    assert await env.svc.users.get(user.id) is not None
    assert f"dy:{user.id}" in env.session.button_data(ADMIN)
    await env.tg.press(ADMIN, f"dy:{user.id}")
    assert await env.svc.users.get(user.id) is None


async def test_send_link_rules(env: Env) -> None:
    nobot = await env.make_user("NoBot", tg_id=USER)  # created by hand, bot not started
    await env.tg.press(ADMIN, f"s:{nobot.id}")
    assert any("бот не запущен" in a for a in env.session.alerts())
    ok = await env.make_user("Ok", tg_id=USER + 1, started=True)
    await env.tg.press(ADMIN, f"s:{ok.id}")
    assert any(env.link_html(ok) in t for t in env.session.texts(USER + 1))
    # blocked bot: 403 clears can_message
    blocked = await env.make_user("Blk", tg_id=USER + 2, started=True)
    env.session.errors[("SendMessage", USER + 2)] = [forbidden(USER + 2)]
    await env.tg.press(ADMIN, f"s:{blocked.id}")
    extra = env.svc.ctx.db.call(repo.get_user_extra, blocked.id)
    assert extra and not extra.can_message


async def test_create_user_dialog(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "Новый <клиент>")
    await env.tg.send(ADMIN, "Иван 🐉 <b>Петров</b>")  # display name step
    await env.tg.send(ADMIN, "abc")  # not a number: asked again
    assert any("число" in t for t in env.session.texts(ADMIN))
    await env.tg.send(ADMIN, "4242")
    await env.tg.press(ADMIN, "ct:1y")
    runs = len(env.svc.runs())
    await env.tg.send(ADMIN, "friend")
    assert len(env.svc.runs()) == runs + 1
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, 4242)
    assert user and user.name == "Новый <клиент>" and user.comment == "friend"
    assert user.display_name == "Иван 🐉 <b>Петров</b>"
    assert user.expires_at is not None
    assert any(env.link_html(user) in t for t in env.session.texts(ADMIN))
    assert env.session.photos(ADMIN)


async def test_create_user_dialog_skips(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "Minimal")
    await env.tg.press(ADMIN, "cs:dn")
    await env.tg.press(ADMIN, "cs:tg")
    await env.tg.press(ADMIN, "ct:df")
    await env.tg.press(ADMIN, "cs:cm")
    user = env.svc.ctx.db.call(repo.all_users)[0]
    assert user.name == "Minimal" and user.tg_id is None and user.comment == ""
    assert user.display_name == ""
    assert any(env.link_html(user) in t for t in env.session.texts(ADMIN))


async def test_cancel_clears_dialog(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "/cancel")
    await env.tg.send(ADMIN, "stray text")
    assert env.svc.ctx.db.call(repo.all_users) == []


async def test_mode_switch_apply_status_and_backup(env: Env) -> None:
    await env.tg.press(ADMIN, "md")
    assert "по одобрению" in env.session.texts(ADMIN)[0]
    await env.tg.press(ADMIN, "ms:open")
    assert env.svc.ctx.db.call(repo.get_setting, "issuance_mode") == "open"
    await env.tg.press(ADMIN, "ap")
    assert "#1" in env.session.texts(ADMIN)[-1]
    await env.tg.press(ADMIN, "bk")
    assert len(env.svc.ctx.db.call(repo.list_backups)) >= 1
    assert "Бэкап создан" in env.session.texts(ADMIN)[-1]


async def test_requests_list(env: Env) -> None:
    await env.tg.press(ADMIN, "rq")
    assert "Заявок нет" in env.session.texts(ADMIN)[0]
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    env.session.clear()
    await env.tg.press(ADMIN, "rq")
    assert "ra:1" in env.session.button_data(ADMIN)


async def test_broadcast_dialog_with_exclusions_and_report(env: Env) -> None:
    a = await env.make_user("A", tg_id=USER, started=True)
    await env.make_user("B", tg_id=USER + 1)  # bot not started
    await env.make_user("C")  # no telegram id
    await env.tg.press(ADMIN, "bca")
    await env.tg.send(ADMIN, "-")
    preview = env.session.texts(ADMIN)[-1]
    assert "Получат: 1. Исключены: 2" in preview
    assert "бот не запущен" in preview and "не указан Telegram ID" in preview
    await env.tg.press(ADMIN, "bgo")
    await env.tg.press(ADMIN, "bgo")  # double click: still one broadcast
    await env.deps.wait_background()
    assert len(env.sender.to(USER)) == 1
    button = env.sender.to(USER)[0].button
    assert button is not None and button.url == env.svc.users.link(a)
    assert "отправлено 1" in env.session.texts(ADMIN)[-1]
    await env.tg.press(ADMIN, "br:1")
    report = env.session.texts(ADMIN)[-1]
    assert "A" in report and "бот не запущен" in report
    assert html.unescape(report)


async def test_broadcast_selected_only(env: Env) -> None:
    a = await env.make_user("A", tg_id=USER, started=True)
    await env.make_user("B", tg_id=USER + 1, started=True)
    await env.tg.press(ADMIN, f"sel:{a.id}")
    await env.tg.press(ADMIN, "bcs")
    await env.tg.send(ADMIN, "Привет, {name}!")
    await env.tg.press(ADMIN, "bgo")
    await env.deps.wait_background()
    assert [s.chat_id for s in env.sender.sent] == [USER]
    assert "Привет, A!" in env.sender.sent[0].text


async def test_broadcast_without_selection_refused(env: Env) -> None:
    await env.tg.press(ADMIN, "bcs")
    assert any("Никто не выбран" in a for a in env.session.alerts())
