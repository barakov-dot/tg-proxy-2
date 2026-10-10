# ruff: noqa: RUF001
from __future__ import annotations

import re

from tests.web.conftest import Web
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import NewUser

REVEAL_BTN = re.compile(r"/users/\d+/reveal")


async def flash_after(w: Web, r) -> str:  # type: ignore[no-untyped-def]
    assert r.status_code == 303, r.text[:300]
    page = await w.client.get(r.headers["location"])
    m = re.search(r'<div class="alert (?:ok|err)" role="status">([^<]*)</div>', page.text)
    return m.group(1) if m else ""


def statuses(w: Web) -> dict[str, UserStatus]:
    return {u.name: u.status for u in w.ctx.db.call(repo.all_users)}


async def test_bulk_selected_ids(aw: Web) -> None:
    ids = await aw.create_users("a", "b", "c")
    r = await aw.post("/users/bulk", {"action": "disable", "ids": [str(ids[0]), str(ids[1])]})
    assert "Готово: 2" in await flash_after(aw, r)
    assert statuses(aw) == {
        "a": UserStatus.DISABLED,
        "b": UserStatus.DISABLED,
        "c": UserStatus.ACTIVE,
    }
    await aw.post("/users/bulk", {"action": "enable", "ids": [str(ids[0])]})
    assert statuses(aw)["a"] is UserStatus.ACTIVE


async def test_bulk_all_matching_filter(aw: Web) -> None:
    await aw.ctx.users.create([NewUser(name=f"grp{i:02d}") for i in range(60)], "t")
    await aw.create_users("other")
    form = {"action": "disable", "all_matching": "1", "filter_qs": "q=grp", "expected_total": "60"}
    r = await aw.post("/users/bulk", form)
    assert "Готово: 60" in await flash_after(aw, r)
    st = statuses(aw)
    assert sum(1 for s in st.values() if s is UserStatus.DISABLED) == 60
    assert st["other"] is UserStatus.ACTIVE


async def test_bulk_all_matching_refuses_when_selection_changed(aw: Web) -> None:
    await aw.create_users("x1", "x2")
    form = {"action": "disable", "all_matching": "1", "filter_qs": "q=x", "expected_total": "5"}
    assert "изменилась" in await flash_after(aw, await aw.post("/users/bulk", form))
    assert set(statuses(aw).values()) == {UserStatus.ACTIVE}


async def test_bulk_filter_whitelisting_in_post(aw: Web) -> None:
    await aw.create_users("x1")
    r = await aw.post(
        "/users/bulk",
        {"action": "disable", "all_matching": "1", "filter_qs": "sort=id;DROP TABLE users"},
    )
    assert r.status_code == 400
    assert len(aw.ctx.db.call(repo.all_users)) == 1


async def test_bulk_extend_comment_delete_and_none(aw: Web) -> None:
    ids = await aw.create_users("a", "b")
    before = [u.expires_at for u in aw.ctx.db.call(repo.all_users)]
    r = await aw.post(
        "/users/bulk", {"action": "extend", "days": "10", "ids": [str(i) for i in ids]}
    )
    assert "Готово" in await flash_after(aw, r)
    after = [u.expires_at for u in aw.ctx.db.call(repo.all_users)]
    assert all(a > b for a, b in zip(after, before, strict=True))  # type: ignore[operator]
    await aw.post(
        "/users/bulk", {"action": "set_comment", "comment": "группа", "ids": [str(i) for i in ids]}
    )
    assert {u.comment for u in aw.ctx.db.call(repo.all_users)} == {"группа"}
    assert "Ничего не выбрано" in await flash_after(
        aw, await aw.post("/users/bulk", {"action": "disable"})
    )
    r = await aw.post("/users/bulk", {"action": "delete", "ids": [str(ids[0])]})
    assert "введите слово" in await flash_after(aw, r)
    assert len(aw.ctx.db.call(repo.all_users)) == 2
    r = await aw.post(
        "/users/bulk", {"action": "delete", "confirm": "удалить", "ids": [str(ids[0])]}
    )
    assert "Удалено пользователей: 1" in await flash_after(aw, r)
    assert [u.name for u in aw.ctx.db.call(repo.all_users)] == ["b"]


async def test_bulk_send_link_uses_bot_port(aw: Web) -> None:
    ids = await aw.create_users("a", "b")
    r = await aw.post("/users/bulk", {"action": "send_link", "ids": [str(i) for i in ids]})
    assert "отправлены: 2" in await flash_after(aw, r)
    assert aw.broadcast.sent_links == [ids]


async def test_bulk_failure_shows_error_and_keeps_state(aw: Web) -> None:
    ids = await aw.create_users("a")
    aw.fake.fail_on("systemctl", "restart tproxy-server")
    r = await aw.post("/users/bulk", {"action": "disable", "ids": [str(ids[0])]})
    assert "изменения отменены" in await flash_after(aw, r)
    assert statuses(aw)["a"] is UserStatus.ACTIVE


# ----------------------------------------------------------------------------- create


async def test_create_single_waits_for_apply_and_links_hidden_until_reveal(aw: Web) -> None:
    r = await aw.post(
        "/users/new", {"mode": "single", "name": "alice", "tg_id": "1234", "term": "default"}
    )
    assert r.status_code == 200 and "Создано пользователей: 1" in r.text
    assert REVEAL_BTN.search(r.text)
    aw.assert_no_secret(r.text)
    assert "t.me/webproxy" not in r.text
    (user,) = aw.ctx.db.call(repo.all_users)
    assert user.tg_id == 1234
    # the apply finished before the page was produced: the profile exists on the relay
    assert any(
        p["name"] == "u1" for p in aw.fake.get_json("/etc/tproxy-server/profiles.json")["profiles"]
    )
    rv = await aw.post("/users/1/reveal")
    assert rv.status_code == 200
    assert f"https://t.me/webproxy?server=proxy.example.com&amp;secret={user.secret}" in rv.text
    assert "<svg" in rv.text and "tg://webproxy" in rv.text


async def test_create_failure_shows_error_without_links(aw: Web) -> None:
    aw.fake.fail_on("systemctl", "restart tproxy-server")
    r = await aw.post("/users/new", {"mode": "single", "name": "alice", "term": "default"})
    assert r.status_code == 409 and "изменения отменены" in r.text
    assert not REVEAL_BTN.search(r.text) and "t.me" not in r.text
    assert aw.ctx.db.call(repo.all_users) == []
    aw.assert_no_secret(r.text)


async def test_create_batch_by_count_single_apply(aw: Web) -> None:
    runs_before = len(aw.ctx.db.call(repo.list_apply_runs, 100))
    r = await aw.post("/users/new", {"mode": "count", "prefix": "vip", "count": "12", "term": "1y"})
    assert r.status_code == 200 and "Создано пользователей: 12" in r.text
    names_ = sorted(u.name for u in aw.ctx.db.call(repo.all_users))
    assert names_[0] == "vip-01" and names_[-1] == "vip-12"
    assert len(aw.ctx.db.call(repo.list_apply_runs, 100)) == runs_before + 1
    assert len(set(REVEAL_BTN.findall(r.text))) == 12


async def test_create_batch_by_list(aw: Web) -> None:
    text = "ann; 111; first; Анна 🐉\n# skip\nbob;;\ncarl; 333; с точкой; 山田"
    r = await aw.post("/users/new", {"mode": "list", "list": text, "term": "1m"})
    assert r.status_code == 200
    users = {u.name: u for u in aw.ctx.db.call(repo.all_users)}
    assert users["ann"].tg_id == 111 and users["ann"].comment == "first"
    assert users["ann"].display_name == "Анна 🐉" and users["bob"].display_name == ""
    assert users["bob"].tg_id is None and users["carl"].comment == "с точкой"
    assert users["carl"].display_name == "山田" and users["carl"].name == "carl"


async def test_create_validation_errors(aw: Web) -> None:
    cases = [
        {"mode": "single", "name": ""},
        {"mode": "count", "prefix": "", "count": "3"},
        {"mode": "count", "prefix": "a", "count": "0"},
        {"mode": "count", "prefix": "a", "count": "5000"},
        {"mode": "list", "list": " \n"},
        {"mode": "list", "list": "x; abc"},
        {"mode": "single", "name": "a", "term": "date", "term_date": "bad"},
        {"mode": "single", "name": "a", "term": "bogus"},
        {"mode": "bogus", "name": "a"},
    ]
    for case in cases:
        r = await aw.post("/users/new", {"term": "default", **case})
        assert r.status_code == 422, case
    assert aw.ctx.db.call(repo.all_users) == []


async def test_create_duplicate_name_shows_service_error(aw: Web) -> None:
    await aw.create_users("dup")
    r = await aw.post("/users/new", {"mode": "single", "name": "dup", "term": "default"})
    assert r.status_code == 409 and "занято" in r.text


async def test_no_secret_in_any_page_until_revealed(aw: Web) -> None:
    ids = await aw.create_users("alice", "bob")
    for path in (
        "/",
        "/users",
        f"/users/{ids[0]}",
        "/users/new",
        "/import",
        "/requests",
        "/broadcast",
        "/settings",
        "/backups",
        "/audit",
        "/audit?tab=apply",
        f"/users/{ids[0]}/traffic.json",
    ):
        r = await aw.client.get(aw.u(path))
        assert r.status_code == 200, path
        aw.assert_no_secret(r.text)
    # reveal needs auth and CSRF
    assert (await aw.client.post(aw.u(f"/users/{ids[0]}/reveal"))).status_code == 403
    aw.client.cookies.clear()
    assert (await aw.client.post(aw.u(f"/users/{ids[0]}/reveal"))).status_code == 303


async def test_reveal_many_text(aw: Web) -> None:
    ids = await aw.create_users("alice", "bob")
    r = await aw.post("/users/reveal-many", {"ids": [str(i) for i in ids]})
    assert r.status_code == 200 and "alice: https://t.me/webproxy" in r.text
    assert (await aw.post("/users/reveal-many", {})).status_code == 400


async def test_reveal_without_proxy_hostname_gives_error(aw: Web) -> None:
    ids = await aw.create_users("alice")
    aw.ctx.db.call(repo.set_setting, "proxy_hostname", "")
    r = await aw.post(f"/users/{ids[0]}/reveal")
    assert r.status_code == 409 and "proxy_hostname" in r.text
