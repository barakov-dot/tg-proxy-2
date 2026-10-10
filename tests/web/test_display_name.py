"""Web: the "Имя" (display name) field next to the technical "Профиль" (name)."""

from __future__ import annotations

import re

import pytest

from tests.web.conftest import ROOT, Web
from tgpanel.db import repo
from tgpanel.domain.models import UserRecord

LABELS = ["Дмитрий Жабкин", "Инга Базанова 🐉", "山田 太郎", "Макс"]
XSS = "<script>alert(1)</script> \"q\" & 's'"
HX = {"HX-Request": "true"}
DN_LINK = re.compile(r'<a href="[^"]*/users/(\d+)"(?: class="muted")?>([^<]*)</a>')


def users_by_name(w: Web) -> dict[str, UserRecord]:
    return {u.name: u for u in w.ctx.db.call(repo.all_users)}


async def seed(aw: Web) -> None:
    await aw.create_users("user_1", "user_2", "user_3", "user_4")
    for uid, label in zip((1, 2, 3), LABELS, strict=False):
        aw.ctx.db.call(repo.update_user, uid, display_name=label)


@pytest.mark.parametrize("label", LABELS)
async def test_create_single_with_display_name(aw: Web, label: str) -> None:
    r = await aw.post(
        "/users/new",
        {"mode": "single", "name": "user_93455874", "display_name": label, "term": "default"},
    )
    assert r.status_code == 200, r.text[:300]
    (user,) = aw.ctx.db.call(repo.all_users)
    assert (user.name, user.display_name) == ("user_93455874", label)
    assert label in r.text  # shown on the "created" page


async def test_create_single_without_display_name_is_fine(aw: Web) -> None:
    r = await aw.post("/users/new", {"mode": "single", "name": "plain", "term": "default"})
    assert r.status_code == 200
    (user,) = aw.ctx.db.call(repo.all_users)
    assert user.display_name == ""


async def test_create_count_appends_number_to_display_name(aw: Web) -> None:
    r = await aw.post(
        "/users/new",
        {"mode": "count", "prefix": "vip", "count": "3", "display_name": "Гость 🎫", "term": "1m"},
    )
    assert r.status_code == 200
    got = {u.name: u.display_name for u in aw.ctx.db.call(repo.all_users)}
    assert got == {"vip-1": "Гость 🎫 1", "vip-2": "Гость 🎫 2", "vip-3": "Гость 🎫 3"}


async def test_create_list_fourth_column_and_backward_compatible_short_lines(aw: Web) -> None:
    text = "a; 11; note; Анна 🐉\nb; 22; just a comment\nc\nd;;;山田"
    r = await aw.post("/users/new", {"mode": "list", "list": text, "term": "1m"})
    assert r.status_code == 200
    users = users_by_name(aw)
    assert (users["a"].tg_id, users["a"].comment, users["a"].display_name) == (
        11,
        "note",
        "Анна 🐉",
    )
    assert (users["b"].comment, users["b"].display_name) == ("just a comment", "")
    assert users["c"].display_name == "" and users["d"].display_name == "山田"


async def test_create_rejects_bad_display_names(aw: Web) -> None:
    r = await aw.post(
        "/users/new", {"mode": "single", "name": "a", "display_name": "я" * 101, "term": "default"}
    )
    assert r.status_code == 422
    r = await aw.post("/users/new", {"mode": "list", "list": "a;;;" + "я" * 101, "term": "default"})
    assert r.status_code == 422
    assert aw.ctx.db.call(repo.all_users) == []


@pytest.mark.parametrize("label", LABELS)
async def test_card_edit_display_name_keeps_name(aw: Web, label: str) -> None:
    await aw.create_users("user_93455874")
    page = await aw.client.get(aw.u("/users/1"))
    assert 'name="display_name"' in page.text and 'maxlength="100"' in page.text
    r = await aw.post(
        "/users/1/meta",
        {"name": "user_93455874", "display_name": label, "comment": "", "tg_username": ""},
    )
    assert r.status_code == 303
    user = aw.ctx.db.call(repo.get_user, 1)
    assert user and (user.name, user.display_name) == ("user_93455874", label)
    assert label in (await aw.client.get(aw.u("/users/1"))).text
    # clearing the field clears the label
    await aw.post("/users/1/meta", {"name": "user_93455874", "display_name": "", "comment": ""})
    user = aw.ctx.db.call(repo.get_user, 1)
    assert user and user.display_name == ""


async def test_card_rejects_overlong_display_name(aw: Web) -> None:
    await aw.create_users("x")
    r = await aw.post("/users/1/meta", {"name": "x", "display_name": "я" * 101, "comment": ""})
    assert r.status_code == 303
    user = aw.ctx.db.call(repo.get_user, 1)
    assert user and user.display_name == ""


async def test_table_shows_display_name_first_then_profile_with_fallback(aw: Web) -> None:
    await seed(aw)
    html = (await aw.client.get(aw.u("/users"))).text
    head = re.findall(r"<th><a [^>]*>([^<]+)</a>", html)
    assert head[:3] == ["№", "Имя", "Профиль"]
    assert "Дмитрий Жабкин" in html
    assert "Инга Базанова 🐉" in html and "山田 太郎" in html
    # user 4 has no display name: the technical name is shown in muted style
    assert re.search(r'<a href="[^"]*/users/4" class="muted">user_4</a>', html)
    assert not re.search(r'<a href="[^"]*/users/1" class="muted">', html)


async def test_live_search_matches_display_name_name_and_is_unicode_case_insensitive(
    aw: Web,
) -> None:
    await seed(aw)

    async def found(q: str) -> list[str]:
        r = await aw.client.get(aw.u("/users"), params={"q": q}, headers=HX)
        assert r.status_code == 200 and "<html" not in r.text
        return sorted({i for i, _ in DN_LINK.findall(r.text)})

    assert await found("жабкин") == ["1"]
    assert await found("ИНГА") == ["2"]
    assert await found("🐉") == ["2"]
    assert await found("山田") == ["3"]
    assert await found("user_4") == ["4"]  # the technical name is still searchable
    assert await found("user_") == ["1", "2", "3", "4"]
    assert await found("несуществует") == []


async def test_sort_by_display_name_uses_label_case_insensitively(aw: Web) -> None:
    await aw.create_users("user_1", "user_2", "user_3", "user_4")
    for uid, label in ((1, "бета"), (2, "Альфа"), (3, "Гамма 🐉"), (4, "")):
        aw.ctx.db.call(repo.update_user, uid, display_name=label)
    aw.ctx.db.call(repo.update_user, 4, name="Эхо")  # no display name: sorted by `name`

    async def order(direction: str) -> list[int]:
        r = await aw.client.get(
            aw.u("/users"), params={"sort": "display_name", "dir": direction}, headers=HX
        )
        assert r.status_code == 200
        return [int(i) for i in re.findall(r'name="ids" value="(\d+)"', r.text)]

    assert await order("asc") == [2, 1, 3, 4]  # Альфа, бета, Гамма, Эхо
    assert await order("desc") == [4, 3, 1, 2]


async def test_display_name_column_can_be_hidden_and_name_stays(aw: Web) -> None:
    await seed(aw)
    html = (
        await aw.client.get(aw.u("/users"), params={"cols_set": "1", "col": ["id", "name"]})
    ).text
    head = re.findall(r"<th><a [^>]*>([^<]+)</a>", html)
    assert head == ["№", "Профиль"]


async def test_display_name_is_escaped_everywhere(aw: Web) -> None:
    await aw.create_users("user_x")
    aw.ctx.db.call(repo.update_user, 1, display_name=XSS)
    aw.ctx.db.call(repo.update_user, 1, tg_id=555, tg_username="u", bot_started=True,
                   can_message=True)  # fmt: skip
    aw.ctx.db.call(repo.update_user, 1, last_seen_at=aw.clock.now)
    for path in ("/users", "/users/1", "/"):
        html = (await aw.client.get(aw.u(path))).text
        assert "<script>alert(1)</script>" not in html, path
    hx = (await aw.client.get(aw.u("/users"), headers=HX)).text
    assert "<script>alert(1)</script>" not in hx and "&lt;script&gt;" in hx
    card = (await aw.client.get(aw.u("/users/1"))).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card
    created = await aw.post(
        "/users/new",
        {"mode": "single", "name": "yy", "display_name": XSS, "term": "default"},
    )
    assert "<script>alert(1)</script>" not in created.text
    assert "&lt;script&gt;" in created.text


async def test_reveal_many_uses_shown_name(aw: Web) -> None:
    await seed(aw)
    r = await aw.post("/users/reveal-many", {"ids": ["1", "4"]})
    assert r.status_code == 200
    assert "Дмитрий Жабкин: https://" in r.text and "user_4: https://" in r.text


async def test_name_and_display_name_are_independent(aw: Web) -> None:
    await aw.create_users("alpha")
    await aw.post("/users/1/meta", {"name": "beta", "display_name": "Альфа", "comment": ""})
    user = aw.ctx.db.call(repo.get_user, 1)
    assert user and (user.name, user.display_name) == ("beta", "Альфа")
    await aw.post("/users/1/meta", {"name": "gamma", "display_name": "Альфа", "comment": ""})
    user = aw.ctx.db.call(repo.get_user, 1)
    assert user and (user.name, user.display_name) == ("gamma", "Альфа")


async def test_import_csv_display_names_never_replace_the_profile_name(owner_web: Web) -> None:
    from tests.web.test_import import hidden_form

    w = owner_web
    csv = "user_93455874;;Дмитрий Жабкин;vip\nuser_12345;;Инга Базанова 🐉;\n"
    r = await w.post("/import/preview", {"regex": r"^user_(\d{5,15})$", "csv_text": csv})
    assert r.status_code == 200 and "Дмитрий Жабкин" in r.text and "Инга Базанова 🐉" in r.text
    form = {k: v for k, v in hidden_form(r.text).items() if k != "csrf_token"}
    form["regex"] = r"^user_(\d{5,15})$"
    form["csv_text"] = csv
    form["dn_2"] = XSS  # edit in the preview table
    form["ack_old_bot"] = "1"
    done = await w.post("/import/confirm", form)
    assert done.headers["location"] == ROOT + "/users"
    users = {u.source_profile_name: u for u in w.ctx.db.call(repo.all_users)}
    assert len(users) == 15 and all(u.name == u.source_profile_name for u in users.values())
    assert users["user_93455874"].display_name == "Дмитрий Жабкин"
    assert users["user_12345"].display_name == "Инга Базанова 🐉"
    assert sum(1 for u in users.values() if u.display_name == XSS.strip()) == 1
    page = (await w.client.get(w.u("/users"))).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    # the preview of what remains never reflects the label unescaped either
    again = await w.post("/import/preview", {"regex": r"^user_(\d{5,15})$", "csv_text": csv})
    assert "<script>alert(1)</script>" not in again.text


async def test_first_column_is_a_sequential_row_number_not_the_database_id(aw) -> None:  # type: ignore[no-untyped-def]
    import re

    for n in range(4):
        r = await aw.post("/users/new", {"mode": "single", "name": f"u{n}", "term": "default"})
        assert r.status_code == 200
    await aw.post("/users/bulk", {"action": "delete", "ids": ["2"], "confirm": "удалить"})
    r = await aw.client.get(aw.u("/users"))
    body = r.text
    numbers = re.findall(r'<td class="nowrap">\s*(\d+)\s*</td>', body)
    assert numbers[:3] == ["1", "2", "3"], numbers  # no gap although database id 2 was deleted
