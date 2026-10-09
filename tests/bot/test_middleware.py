from __future__ import annotations

import logging

import pytest

from tests.apply.conftest import SECRET_RE
from tests.bot.conftest import ADMIN, USER, Env


async def test_group_chats_are_ignored(env: Env) -> None:
    await env.tg.send(ADMIN, "/admin", chat_type="group")
    await env.tg.press(ADMIN, "m", chat_type="supergroup")
    assert env.session.calls == []


async def test_throttle_drops_fast_events(env: Env) -> None:
    now = [0.0]
    env.deps.throttle_interval = 1.0
    env.deps.monotonic = lambda: now[0]
    await env.tg.send(USER, "/start")
    n = len(env.session.calls)
    await env.tg.send(USER, "/start")
    assert len(env.session.calls) == n + 1  # told "too fast" ...
    assert "Слишком часто" in env.session.texts(USER)[-1]
    await env.tg.send(USER, "/start")
    await env.tg.send(USER, "/start")
    assert len(env.session.calls) == n + 1  # ... only once per burst
    now[0] = 2.0
    await env.tg.send(USER, "/start")
    assert len(env.session.calls) > n


async def test_logs_never_contain_texts_links_or_secrets(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await env.make_user("Secretive", tg_id=USER, started=True)
    await env.tg.send(USER, "/start")
    await env.tg.press(USER, "my")
    await env.tg.send(ADMIN, "private message text 12345")
    await env.tg.press(ADMIN, "bgo")
    log_text = caplog.text
    assert user.secret not in log_text
    assert not SECRET_RE.search(log_text)
    assert "t.me/webproxy" not in log_text
    assert "private message text" not in log_text


async def test_handler_error_is_reported_without_details(env: Env) -> None:
    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("secret detail")

    env.deps.traffic.user_totals = boom  # type: ignore[method-assign]
    user = await env.make_user("X", tg_id=USER, started=True)
    await env.tg.press(ADMIN, f"c:{user.id}")  # traffic errors degrade to "нет данных"
    assert "нет данных" in env.session.texts(ADMIN)[0]
    env.deps.users.get = boom  # type: ignore[method-assign]
    await env.tg.press(ADMIN, f"c:{user.id}")
    assert any("Произошла ошибка" in a for a in env.session.alerts())
    assert not any("secret detail" in a for a in env.session.alerts())
