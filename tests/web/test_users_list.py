from __future__ import annotations

import re
from datetime import timedelta
from typing import get_args

import pytest

from tests.web.conftest import Web
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.domain.queries import SortField
from tgpanel.services.api import NewUser

NAME_RE = re.compile(r'<a href="[^"]*/users/\d+">([^<]+)</a>')


def names(html: str) -> list[str]:
    return [n for n in NAME_RE.findall(html) if n != "открыть"]


@pytest.fixture
async def seeded(aw: Web) -> Web:
    ids = await aw.create_users("alice", "bob", "carol", "dave")
    now = aw.clock.now
    db = aw.ctx.db
    db.call(repo.update_user, ids[0], tg_id=1001, comment="vip")
    db.call(repo.update_user, ids[1], comment="import")
    db.call(lambda c: c.execute("UPDATE users SET imported = 1 WHERE id = ?", (ids[1],)))
    db.call(repo.update_user, ids[2], tg_id=1003, status=UserStatus.DISABLED)
    db.call(
        repo.update_user,
        ids[3],
        tg_id=1004,
        tg_username="dave_x",
        bot_started=True,
        expires_at=now + timedelta(days=3),
        last_seen_at=now - timedelta(days=1),
        first_seen_at=now - timedelta(days=5),
    )
    db.call(
        repo.add_traffic,
        "day",
        ids[3],
        now - timedelta(days=1),
        bytes_up=3 << 20,
        bytes_down=2 << 20,
    )
    return aw


async def listing(w: Web, query: str = "") -> list[str]:
    r = await w.client.get(w.u("/users" + ("?" + query if query else "")))
    assert r.status_code == 200, r.text[:200]
    return names(r.text)


@pytest.mark.parametrize("sort", get_args(SortField))
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_every_column_sorts(seeded: Web, sort: str, direction: str) -> None:
    got = await listing(seeded, f"sort={sort}&dir={direction}")
    assert sorted(got) == ["alice", "bob", "carol", "dave"]


async def test_sort_order_by_name_and_id(seeded: Web) -> None:
    assert await listing(seeded, "sort=name&dir=desc") == ["dave", "carol", "bob", "alice"]
    assert await listing(seeded, "sort=id&dir=desc") == ["dave", "carol", "bob", "alice"]
    assert (await listing(seeded, "sort=traffic&dir=desc"))[0] == "dave"
    assert (await listing(seeded, "sort=tg_id&dir=asc"))[:2] == ["alice", "carol"]


@pytest.mark.parametrize(
    "query",
    ["sort=nope", "sort=id;DROP TABLE users", "sort=id%20DESC--", "dir=sideways", "period=1y"],
)
async def test_unknown_sort_or_period_is_400_and_harmless(seeded: Web, query: str) -> None:
    r = await seeded.client.get(seeded.u("/users?" + query))
    assert r.status_code == 400
    assert len(seeded.ctx.db.call(repo.all_users)) == 4


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=alice", {"alice"}),
        ("q=1003", {"carol"}),
        ("q=dave_x", {"dave"}),
        ("status=disabled", {"carol"}),
        ("status=active&status=disabled", {"alice", "bob", "carol", "dave"}),
        ("imported=1", {"bob"}),
        ("imported=0", {"alice", "carol", "dave"}),
        ("tg=0", {"bob"}),
        ("tg=1", {"alice", "carol", "dave"}),
        ("bot=1", {"dave"}),
        ("bot=0", {"alice", "bob", "carol"}),
        ("online=1", set()),
        ("online=0", {"alice", "bob", "carol", "dave"}),
        ("expires_days=5", {"dave"}),
        ("created_from=2026-03-10", {"alice", "bob", "carol", "dave"}),
        ("created_from=2030-01-01", set()),
        ("created_to=2020-01-01", set()),
        ("created_to=2030-01-01", {"alice", "bob", "carol", "dave"}),
        ("seen_from=2026-03-01", {"dave"}),
        ("seen_to=2020-01-01", set()),
        ("traffic_min=1", {"dave"}),
        ("traffic_max=1", {"alice", "bob", "carol"}),
        ("comment=import", {"bob"}),
        ("q=%25", set()),
        ("q=%27%20OR%201%3D1--", set()),
        ("comment=%27%3B%20DROP%20TABLE%20users%3B--", set()),
    ],
)
async def test_each_filter(seeded: Web, query: str, expected: set[str]) -> None:
    assert set(await listing(seeded, query)) == expected
    assert len(seeded.ctx.db.call(repo.all_users)) == 4


@pytest.mark.parametrize(
    "query",
    [
        "online=2",
        "expires_days=abc",
        "created_from=yesterday",
        "traffic_min=x",
        "status=bogus",
        "per=77",
        "page=0",
        "page=abc",
    ],
)
async def test_bad_filter_values_rejected(seeded: Web, query: str) -> None:
    assert (await seeded.client.get(seeded.u("/users?" + query))).status_code == 400


async def test_period_changes_traffic_window(seeded: Web) -> None:
    html = (await seeded.client.get(seeded.u("/users?period=24h"))).text
    assert "Σ 5.0 МБ" not in html
    html = (await seeded.client.get(seeded.u("/users?period=7d"))).text
    assert "Σ 5.0 МБ" in html


async def test_pagination_and_page_sizes(aw: Web) -> None:
    await aw.ctx.users.create(
        [NewUser(name=f"u{i:03d}") for i in range(120)],
        "system",
    )
    assert len(await listing(aw, "sort=name")) == 50
    assert len(await listing(aw, "sort=name&page=3")) == 20
    assert len(await listing(aw, "per=100")) == 100
    assert len(await listing(aw, "per=200")) == 120
    assert (await aw.client.get(aw.u("/users?page=9"))).status_code == 404
    html = (await aw.client.get(aw.u("/users?q=u0&sort=name&page=2"))).text
    assert "q=u0" in html and "sort=name" in html  # state lives in the URL


async def test_column_selection_persisted_in_cookie(seeded: Web) -> None:
    r = await seeded.client.get(seeded.u("/users?cols_set=1&col=id&col=name"))
    assert "tgp_cols=" in r.headers["set-cookie"]
    html = (await seeded.client.get(seeded.u("/users"))).text
    head = html.split("<tbody>")[0]
    assert "Комментарий</a>" not in head and "Имя" in head


async def test_inline_comment_edit_htmx(seeded: Web) -> None:
    r = await seeded.post(
        "/users/1/comment", {"inline_comment": "новый"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and 'value="новый"' in r.text
    assert (await seeded.ctx.users.get(1)).comment == "новый"  # type: ignore[union-attr]
    r = await seeded.post("/users/1/comment", {"inline_comment": "x" * 3000})
    assert r.status_code == 422 and "bad" in r.text


async def test_html_escaping_of_name_and_comment(aw: Web) -> None:
    res = await aw.ctx.users.create(
        [NewUser(name="<script>alert(1)</script>", comment='"><img src=x onerror=alert(2)>')], "t"
    )
    assert res.ok
    for path in ("/users", "/users/1", "/audit"):
        html = (await aw.client.get(aw.u(path))).text
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in (await aw.client.get(aw.u("/users"))).text
