# ruff: noqa: RUF001
from __future__ import annotations

from tests.web.conftest import Web
from tgpanel.db import repo
from tgpanel.web.deps import LinkSendResult


async def test_form_has_checkbox_checked_by_default(aw: Web) -> None:
    page = await aw.client.get(aw.u("/users/new"))
    assert 'name="send_link"' in page.text
    assert 'name="send_link" value="1" checked' in page.text


async def test_single_create_with_tg_id_sends_link_after_apply(aw: Web) -> None:
    r = await aw.post(
        "/users/new",
        {"mode": "single", "term": "default", "name": "alice", "tg_id": "4242", "send_link": "1"},
    )
    assert r.status_code == 200
    (user,) = aw.ctx.db.call(repo.all_users)
    assert aw.broadcast.sent_links == [[user.id]]
    assert "Доставка в Telegram" in r.text and "отправлена" in r.text


async def test_checkbox_off_sends_nothing(aw: Web) -> None:
    r = await aw.post(
        "/users/new", {"mode": "single", "term": "default", "name": "bob", "tg_id": "4243"}
    )
    assert r.status_code == 200
    assert aw.broadcast.sent_links == []
    assert "Доставка в Telegram" not in r.text


async def test_no_tg_id_sends_nothing(aw: Web) -> None:
    await aw.post(
        "/users/new", {"mode": "single", "term": "default", "name": "carol", "send_link": "1"}
    )
    assert aw.broadcast.sent_links == []


async def test_list_create_sends_per_row_with_an_id(aw: Web) -> None:
    text = "a1; 101\na2\na3; 103; note"
    r = await aw.post(
        "/users/new", {"mode": "list", "term": "default", "list": text, "send_link": "1"}
    )
    assert r.status_code == 200
    users = {u.name: u.id for u in aw.ctx.db.call(repo.all_users)}
    assert aw.broadcast.sent_links == [[users["a1"], users["a3"]]]


async def test_failed_create_sends_nothing(aw: Web) -> None:
    await aw.post(
        "/users/new",
        {"mode": "single", "term": "default", "name": "dup", "tg_id": "7", "send_link": "1"},
    )
    aw.broadcast.sent_links.clear()
    r = await aw.post(
        "/users/new",
        {"mode": "single", "term": "default", "name": "dup", "tg_id": "8", "send_link": "1"},
    )
    assert r.status_code == 409
    assert aw.broadcast.sent_links == []


async def test_delivery_failures_are_shown_per_user(aw: Web) -> None:
    async def send(user_ids, actor):  # type: ignore[no-untyped-def]
        return [
            LinkSendResult(i, False, "Бот не запущен у пользователя — ссылка будет отправлена")
            for i in user_ids
        ]

    aw.broadcast.send_links = send  # type: ignore[method-assign]
    r = await aw.post(
        "/users/new",
        {"mode": "single", "term": "default", "name": "dave", "tg_id": "9", "send_link": "1"},
    )
    assert r.status_code == 200
    assert "Бот не запущен у пользователя" in r.text
    assert aw.ctx.db.call(repo.get_user_by_tg_id, 9) is not None  # creation is not undone
