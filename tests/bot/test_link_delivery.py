from __future__ import annotations

import logging

import pytest

from tests.apply.conftest import SECRET_RE
from tests.bot.conftest import ADMIN, USER, Env
from tests.bot.helpers import forbidden, retry_after
from tgpanel.bot.broadcast_port import RequestsPortAdapter
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.services.api import NewUser


def pools(env: Env) -> int:
    return len(env.svc.ctx.db.call(repo.list_pools))


async def new_pool_setup(env: Env) -> None:
    """One secret per process: every new user opens a NEW pool (a slower apply)."""
    env.set_setting("secrets_per_process", "1")
    await env.make_user("filler", tg_id=None)
    assert pools(env) == 1


def link_messages(env: Env, chat: int) -> list[str]:
    users = env.svc.ctx.db.call(repo.get_user_by_tg_id, chat)
    assert users is not None
    needle = env.link_html(users)
    return [t for t in env.session.texts(chat) if needle in t]


async def request_and_approve(env: Env, *, answers_fail: bool = False) -> None:
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    await env.tg.press(ADMIN, "ra:1")
    env.session.fail_answers = answers_fail
    await env.tg.press(ADMIN, "rt:1:1m")
    env.session.fail_answers = False


# ------------------------------------------------------------------ (a) bot approval


async def test_bot_approval_new_pool_delivers_once(env: Env) -> None:
    await new_pool_setup(env)
    await request_and_approve(env)
    assert pools(env) == 2
    assert len(link_messages(env, USER)) == 1
    assert len(env.session.photos(USER)) == 1


async def test_bot_approval_link_delivered_even_if_every_answer_is_rejected(env: Env) -> None:
    """Root cause of the real incident: 'query is too old' on answerCallbackQuery."""
    await new_pool_setup(env)
    await request_and_approve(env, answers_fail=True)
    assert pools(env) == 2
    assert len(link_messages(env, USER)) == 1
    assert "создан" in env.session.texts(ADMIN)[-1].lower()  # admin screen still updated


async def test_bot_approval_link_delivered_even_if_admin_screen_cannot_be_edited(
    env: Env,
) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    env.session.errors[("EditMessageText", ADMIN)] = [
        TelegramBadRequest(method=EditMessageText(text="x"), message="Bad Request: gone")
    ]
    env.session.fail_answers = True
    await env.tg.press(ADMIN, "rt:1:1m")
    assert len(link_messages(env, USER)) == 1


async def test_bot_approval_failed_apply_sends_nothing(env: Env) -> None:
    await new_pool_setup(env)

    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    env.svc.ctx.pipeline._execute = boom  # type: ignore[method-assign]
    await env.tg.press(ADMIN, "rt:1:1m")
    assert not [t for t in env.session.texts(USER) if "t.me/webproxy" in t]
    assert not env.session.photos(USER)
    assert env.notifier.messages
    assert env.svc.ctx.db.call(repo.get_user_by_tg_id, USER) is None


async def test_bot_approval_requester_retry_after_then_ok(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    await env.tg.press(ADMIN, "ra:1")
    env.session.errors[("SendMessage", USER)] = [retry_after(3, USER)]
    await env.tg.press(ADMIN, "rt:1:1m")
    assert len(link_messages(env, USER)) == 1
    assert env.sender.time.now >= 3.0  # waited for the RetryAfter


async def test_bot_approval_undeliverable_is_reported_and_delivered_at_next_start(
    env: Env,
) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    env.session.errors[("SendMessage", USER)] = [forbidden(USER)]
    await env.tg.press(ADMIN, "rt:1:1m")
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user
    assert link_messages(env, USER) == []
    assert "не доставлена" in env.session.texts(ADMIN)[-1]
    assert f"s:{user.id}" in env.session.button_data(ADMIN)  # retry button
    assert user.id in await env.deps.link_delivery.pending_ids()
    await env.tg.send(USER, "/start")  # the user comes back
    assert len(link_messages(env, USER)) == 1
    assert await env.deps.link_delivery.pending_ids() == []
    await env.tg.send(USER, "/start")
    assert len(link_messages(env, USER)) == 1  # not again


# ------------------------------------------------------------------ (b) web approval


async def test_web_approval_new_pool_retry_after_then_ok(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    rp = RequestsPortAdapter(
        env.deps.requests, env.svc.users, env.svc.ctx.db, env.deps.messenger, env.deps.link_delivery
    )
    await env.tg.press(USER, "req")
    env.sender.script[USER] = [retry_after(2, USER)]
    res = await rp.approve(1, Term.MONTH, "web:admin")
    assert res.ok and res.error is None
    assert pools(env) == 2
    sent = env.sender.to(USER)
    assert len(sent) == 1 and sent[0].button is not None
    assert env.sender.time.now >= 2.0


async def test_web_approval_undeliverable_reports_and_waits_for_start(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    rp = RequestsPortAdapter(
        env.deps.requests, env.svc.users, env.svc.ctx.db, env.deps.messenger, env.deps.link_delivery
    )
    await env.tg.press(USER, "req")
    env.sender.script[USER] = [forbidden(USER)]
    res = await rp.approve(1, Term.MONTH, "web:admin")
    assert res.ok and res.error  # approved, but the web UI is told delivery failed
    assert env.sender.to(USER) == []
    await env.tg.send(USER, "/start")
    assert len(link_messages(env, USER)) == 1


# ------------------------------------------------------------------ (c) open mode batch


async def test_open_mode_batch_with_new_pools_and_name_collision(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "open")
    env.set_setting("open_mode_batch_window_s", "5")
    env.deps.requests._sleep = env.sender.time.sleep
    import asyncio

    await asyncio.gather(*(env.tg.press(USER + i, "req", name="Same Name") for i in range(3)))
    assert pools(env) == 4
    for i in range(3):
        assert len(link_messages(env, USER + i)) == 1, i


async def test_open_mode_record_failure_still_delivers(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "open")
    env.set_setting("open_mode_batch_window_s", "0")

    async def broken(*a: object, **k: object) -> None:
        raise RuntimeError("db busy")

    env.deps.requests._finalize = broken  # type: ignore[method-assign]
    await env.tg.press(USER, "req")
    assert len(link_messages(env, USER)) == 1


# ------------------------------------------------------------------ (d) admin dialog


async def create_via_dialog(env: Env, tg_id: int) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, f"Client {tg_id}")
    await env.tg.press(ADMIN, "cs:dn")
    await env.tg.send(ADMIN, str(tg_id))
    await env.tg.press(ADMIN, "ct:df")
    await env.tg.press(ADMIN, "cs:cm")


async def test_dialog_user_who_asked_for_access_gets_the_link_at_once(env: Env) -> None:
    await new_pool_setup(env)
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    await env.tg.press(ADMIN, "rr:1")  # rejected: no profile, but the chat exists
    await create_via_dialog(env, USER)
    assert pools(env) == 2
    assert len(env.sender.to(USER)) == 1
    assert any("Ссылка отправлена" in t for t in env.session.texts(ADMIN))
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user
    extra = env.svc.ctx.db.call(repo.get_user_extra, user.id)
    assert extra and extra.bot_started and extra.can_message


async def test_dialog_unknown_user_gets_link_at_first_start(env: Env) -> None:
    await new_pool_setup(env)
    await create_via_dialog(env, USER + 1)
    assert env.sender.to(USER + 1) == []
    assert any("при первом /start" in t for t in env.session.texts(ADMIN))
    await env.tg.send(USER + 1, "/start")
    assert len(link_messages(env, USER + 1)) == 1
    assert await env.deps.link_delivery.pending_ids() == []


async def test_dialog_without_tg_id_sends_nothing(env: Env) -> None:
    await env.tg.press(ADMIN, "cr")
    await env.tg.send(ADMIN, "NoTg")
    await env.tg.press(ADMIN, "cs:dn")
    await env.tg.press(ADMIN, "cs:tg")
    await env.tg.press(ADMIN, "ct:df")
    await env.tg.press(ADMIN, "cs:cm")
    assert env.sender.sent == []
    assert [u.name for u in env.svc.ctx.db.call(repo.all_users)][-1] == "NoTg"


# ------------------------------------------------------------------ delivery service


async def test_link_delivery_retries_transient_errors_and_logs_without_links(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started = await env.make_user("S", tg_id=USER, started=True)
    env.sender.script[USER] = [RuntimeError("net")] * 2
    (ok,) = await env.deps.link_delivery.deliver([started.id], "web:admin", default="{link}")
    assert ok.status == "sent" and len(env.sender.to(USER)) == 1
    env.sender.script[USER] = [RuntimeError("net")] * 10
    (bad,) = await env.deps.link_delivery.deliver([started.id], "web:admin", default="{link}")
    assert bad.status == "error" and "доставки" in bad.note
    assert len(env.sender.to(USER)) == 1  # nothing more went out
    assert started.secret not in caplog.text and not SECRET_RE.search(caplog.text)
    assert "t.me/webproxy" not in caplog.text
    assert bad.note.count("http") == 0


async def test_link_delivery_statuses(env: Env) -> None:
    ld = env.deps.link_delivery
    nobody = await env.make_user("NoTg")
    unstarted = await env.make_user("Un", tg_id=USER + 1)
    off = await env.make_user("Off", tg_id=USER + 2, started=True)
    await env.svc.users.set_status([off.id], False, "web:admin")
    out = await ld.deliver([nobody.id, unstarted.id, off.id, 9999], "web:admin", default="{link}")
    assert [r.status for r in out] == ["skipped", "pending", "skipped", "skipped"]
    assert await ld.pending_ids() == [unstarted.id]
    assert env.sender.sent == []


async def test_pending_flag_for_created_batch_via_service(env: Env) -> None:
    res = await env.svc.users.create([NewUser(name="B1", tg_id=USER)], "web:admin")
    assert res.ok
    (r,) = await env.deps.link_delivery.deliver(list(res.user_ids), "web:admin", default="{link}")
    assert r.status == "pending" and "при первом /start" in r.note
