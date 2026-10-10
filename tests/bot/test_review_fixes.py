from __future__ import annotations

import asyncio
import logging

import pytest

from tests.bot.conftest import ADMIN, USER, Env
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus

BOT_TOKEN = "123456789:" + "A" * 35


# ------------------------------------------------------------------ W3: non-ASCII digits


async def test_non_ascii_digits_are_rejected_not_crashing(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "Name")
    await env.tg.press(ADMIN, "cs:dn")
    await env.tg.send(ADMIN, "١٢٣")  # Arabic-Indic digits: str.isdigit() is True
    await env.tg.send(ADMIN, "²")
    assert sum("Нужно положительное число" in t for t in env.session.texts(ADMIN)) == 2
    assert not env.session.alerts()
    await env.tg.press(ADMIN, "bla")
    await env.tg.send(ADMIN, "١٢٣")
    assert "Нужен положительный" in env.session.texts(ADMIN)[-1]
    await env.tg.send(ADMIN, "/ban ٣")
    assert "Использование" in env.session.texts(ADMIN)[-1]


async def test_garbage_blacklist_never_breaks_submit(env: Env) -> None:
    env.set_setting("bot_blacklist", "١٢٣, ², " + "9" * 5000 + ", 77, \x00")
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is not None
    assert await env.deps.requests.is_blacklisted(77)


# ------------------------------------------------------------------ W9: inaccessible messages


async def test_callback_with_inaccessible_message_gets_fresh_message(env: Env) -> None:
    user = await env.make_user("Old", tg_id=USER, started=True)
    await env.tg.press(ADMIN, f"c:{user.id}", inaccessible=True)
    assert "Old" in env.session.texts(ADMIN)[0]
    assert any(n == "AnswerCallbackQuery" for n, _ in env.session.calls)
    assert not [1 for n, _ in env.session.calls if n == "EditMessageText"]


async def test_inaccessible_callback_in_group_is_ignored(env: Env) -> None:
    await env.tg.press(ADMIN, "m", chat_type="supergroup", inaccessible=True)
    assert env.session.calls == []


async def test_user_callback_with_inaccessible_message(env: Env) -> None:
    await env.make_user("U", tg_id=USER, started=True)
    await env.tg.press(USER, "my", inaccessible=True)
    assert env.session.photos(USER)


# ------------------------------------------------------------------ FSM and commands


async def test_start_and_admin_cancel_a_running_dialog(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "/start")
    assert "доступ" in env.session.texts(ADMIN)[-1].lower()
    await env.tg.send(ADMIN, "stray name")
    assert env.svc.ctx.db.call(repo.all_users) == []
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "/admin")
    assert "Админ-панель" in env.session.texts(ADMIN)[-1]
    await env.tg.send(ADMIN, "stray name")
    assert env.svc.ctx.db.call(repo.all_users) == []
    # also from a comment edit and a broadcast text prompt
    await env.tg.press(ADMIN, "bca")
    await env.tg.send(ADMIN, "/admin")
    await env.tg.send(ADMIN, "hello everybody")
    assert env.sender.sent == []


# ------------------------------------------------------------------ idempotent toggle


async def test_toggle_buttons_carry_the_target_state(env: Env) -> None:
    user = await env.make_user("T", tg_id=USER, started=True)
    await env.tg.press(ADMIN, f"c:{user.id}")
    assert f"t:{user.id}:0" in env.session.button_data(ADMIN)
    runs = len(env.svc.runs())
    await env.tg.press(ADMIN, f"t:{user.id}:0")
    await env.tg.press(ADMIN, f"t:{user.id}:0")  # stale second click: no second change
    assert (await env.svc.users.get(user.id)).status is UserStatus.DISABLED  # type: ignore[union-attr]
    assert len(env.svc.runs()) == runs + 1
    assert f"t:{user.id}:1" in env.session.button_data(ADMIN)
    await env.tg.press(ADMIN, f"t:{user.id}")  # legacy format is not accepted
    assert (await env.svc.users.get(user.id)).status is UserStatus.DISABLED  # type: ignore[union-attr]


# ------------------------------------------------------------------ blacklist editing


async def test_blacklist_dialog_and_commands(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(ADMIN, "bl")
    assert "пусто" in env.session.texts(ADMIN)[-1]
    await env.tg.press(ADMIN, "bla")
    await env.tg.send(ADMIN, str(USER))
    assert str(USER) in env.session.texts(ADMIN)[-1]
    await env.tg.press(USER, "req")
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is None
    await env.tg.press(ADMIN, "blr")
    await env.tg.send(ADMIN, str(USER))
    assert "пусто" in env.session.texts(ADMIN)[-1]
    await env.tg.send(ADMIN, "/ban 4242")
    assert await env.deps.requests.blacklist_ids() == [4242]
    await env.tg.send(ADMIN, "/unban 4242")
    assert await env.deps.requests.blacklist_ids() == []
    audit = env.svc.audit_text()
    assert "blacklist.add" in audit and "blacklist.remove" in audit
    # a non-admin cannot edit it
    await env.tg.send(USER, "/ban 5")
    await env.tg.press(USER, "bla")
    assert await env.deps.requests.blacklist_ids() == []


# ------------------------------------------------------------------ message templates


async def test_web_templates_are_used_and_escaped(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    env.set_setting("msg.welcome", "Привет, {name}! <b>x</b> {unknown}")
    await env.tg.send(USER, "/start")
    text = env.session.texts(USER)[0]
    assert text == "Привет, Tester! &lt;b&gt;x&lt;/b&gt; {unknown}"
    env.set_setting("msg.approved", "Одобрено для {name}, до {expires} ({days} дн.)\n{link}")
    await env.tg.press(USER, "req")
    await env.tg.press(ADMIN, "rt:1:1m")
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user
    got = env.session.texts(USER)[-1]
    assert got.startswith("Одобрено для Tester, до ") and "(31 дн.)" in got
    assert env.link_html(user) in got
    env.set_setting("msg.link", "Ваша: {link}")
    await env.tg.press(USER, "my")
    assert env.session.texts(USER)[-1] == f"Ваша: {env.link_html(user)}"
    env.set_setting("msg.rejected", "Нет, {name}")
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER + 1, "req")
    await env.tg.press(ADMIN, "rr:2")
    assert "Нет, Tester" in env.session.texts(USER + 1)[-1]


async def test_default_broadcast_template_from_settings(env: Env) -> None:
    await env.make_user("A", tg_id=USER, started=True)
    env.set_setting("msg.broadcast", "Новость для {name}")
    await env.tg.press(ADMIN, "bca")
    await env.tg.send(ADMIN, "-")
    await env.tg.press(ADMIN, "bgo")
    await env.deps.wait_background()
    assert env.sender.sent[0].text == "Новость для A"


# ------------------------------------------------------------------ background tasks


async def test_spawned_task_failures_are_logged_scrubbed(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    async def boom() -> None:
        raise RuntimeError(f"token {BOT_TOKEN} leaked")

    env.deps.spawn(boom())
    await env.deps.wait_background()
    assert "background task failed" in caplog.text
    assert BOT_TOKEN not in caplog.text and "[redacted]" in caplog.text


async def test_shutdown_logs_and_cancels_unfinished(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    env.deps.spawn(asyncio.sleep(60))
    await asyncio.sleep(0)
    await env.deps.shutdown()
    assert "unfinished at shutdown" in caplog.text
    assert not env.deps.tasks


async def test_broadcast_summary_failure_is_logged_not_raised(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.bot.helpers import forbidden

    caplog.set_level(logging.WARNING)
    await env.make_user("A", tg_id=USER, started=True)
    await env.tg.press(ADMIN, "bca")
    await env.tg.send(ADMIN, "hello")
    env.session.errors[("SendMessage", ADMIN)] = [forbidden(ADMIN)]
    await env.tg.press(ADMIN, "bgo")  # the "started" message uses edit; the summary is blocked
    await env.deps.wait_background()
    assert env.sender.sent  # the broadcast itself went out
