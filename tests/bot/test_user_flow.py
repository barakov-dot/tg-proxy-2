# ruff: noqa: RUF001
from __future__ import annotations

from datetime import timedelta

from tests.bot.conftest import ADMIN, USER, Env
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus


async def test_start_without_profile_offers_request(env: Env) -> None:
    await env.tg.send(USER, "/start")
    assert "req" in env.session.button_data(USER)


async def test_start_active_shows_status_and_link_button(env: Env) -> None:
    user = await env.make_user("Vasya", tg_id=USER)
    await env.tg.send(USER, "/start")
    assert "активен" in env.session.texts(USER)[0]
    assert "my" in env.session.button_data(USER)
    # a hand-created user that never pressed /start is now recognised
    extra = env.svc.ctx.db.call(repo.get_user_extra, user.id)
    assert extra and extra.bot_started and extra.can_message
    await env.tg.press(USER, "my")
    link = env.link_html(user)
    assert any(link in t for t in env.session.texts(USER))
    assert len(env.session.photos(USER)) >= 1


async def test_imported_user_recognised_at_start_gets_link_at_once(env: Env) -> None:
    user = await env.make_user("Old", tg_id=USER)
    before = len(env.svc.ctx.db.call(repo.all_users))
    await env.tg.send(USER, "/start")
    assert len(env.svc.ctx.db.call(repo.all_users)) == before  # no new profile
    assert any(env.link_html(user) in t for t in env.session.texts(USER))
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is None


async def test_disabled_user_gets_no_link(env: Env) -> None:
    user = await env.make_user("Dis", tg_id=USER, started=True)
    await env.svc.users.set_status([user.id], False, "web:admin")
    await env.tg.send(USER, "/start")
    assert "отключён" in env.session.texts(USER)[0]
    await env.tg.press(USER, "my")
    assert not env.session.photos(USER)


async def test_approval_mode_request_goes_to_admins(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    assert env.session.button_data(ADMIN) == ["ra:1", "rr:1"]
    assert "заявка" in env.session.texts(USER)[0].lower()
    # second press: still one pending request
    await env.tg.press(USER, "req")
    assert len(env.svc.ctx.db.call(repo.list_access_requests, "pending")) == 1


async def test_approve_creates_user_with_one_apply_and_sends_link(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    runs = len(env.svc.runs())
    await env.tg.press(ADMIN, "ra:1")
    assert "rt:1:1m" in env.session.button_data(ADMIN)
    await env.tg.press(ADMIN, "rt:1:1m")
    assert len(env.svc.runs()) == runs + 1
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user is not None and user.expires_at is not None
    assert any(env.link_html(user) in t for t in env.session.texts(USER))
    # double click is harmless
    await env.tg.press(ADMIN, "rt:1:1m")
    assert len(env.svc.runs()) == runs + 1
    assert len(env.svc.ctx.db.call(repo.all_users)) == 1


async def test_reject_notifies_user(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    await env.tg.press(USER, "req")
    await env.tg.press(ADMIN, "rr:1")
    assert any("отклонена" in t for t in env.session.texts(USER))
    assert env.svc.ctx.db.call(repo.get_user_by_tg_id, USER) is None
    await env.tg.press(ADMIN, "rr:1")  # second click
    assert len(env.svc.ctx.db.call(repo.list_access_requests, "rejected")) == 1


async def test_open_mode_prepares_then_sends_link(env: Env) -> None:
    env.set_setting("issuance_mode", "open")
    await env.tg.press(USER, "req")
    texts = env.session.texts(USER)
    assert "Готовим" in texts[0]
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user is not None and user.status is UserStatus.ACTIVE
    assert any(env.link_html(user) in t for t in texts)
    assert env.session.button_data(ADMIN) == []  # nobody to approve


async def test_failed_apply_no_link_and_admin_notified(env: Env) -> None:
    env.set_setting("issuance_mode", "open")
    _break_apply(env)
    await env.tg.press(USER, "req")
    assert not env.session.photos(USER)
    texts = "\n".join(env.session.texts(USER))
    assert "https://t.me/webproxy" not in texts
    assert "Не удалось" in texts
    assert env.notifier.messages and "Не удалось применить" in env.notifier.messages[0]
    assert env.svc.ctx.db.call(repo.get_user_by_tg_id, USER) is None


def _break_apply(env: Env) -> None:
    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    env.svc.ctx.pipeline._execute = boom  # type: ignore[method-assign]


async def test_blacklisted_cannot_request(env: Env) -> None:
    env.set_setting("bot_blacklist", f"7, {USER}")
    await env.tg.press(USER, "req")
    assert env.session.button_data(ADMIN) == []
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is None


async def test_rate_limit_after_rejections(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    for rid in (1, 2, 3):
        await env.tg.press(USER, "req")
        await env.tg.press(ADMIN, f"rr:{rid}")
    env.session.clear()
    await env.tg.press(USER, "req")
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is None
    assert "Слишком много" in env.session.texts(USER)[0]
    env.svc.clock.now += timedelta(hours=2)
    await env.tg.press(USER, "req")
    assert env.svc.ctx.db.call(repo.pending_request_for, USER) is not None
