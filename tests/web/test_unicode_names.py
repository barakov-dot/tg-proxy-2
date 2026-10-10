"""User names may contain any text: Cyrillic, emoji, spaces (single line)."""

from __future__ import annotations

import pytest

from tests.web.conftest import Web
from tgpanel.db import repo

NAMES = ["Иван Петров", "Анна 🌸", "😀", "Zoë ñ 中文", "Саша-2"]


@pytest.mark.parametrize("name", NAMES)
async def test_create_single_accepts_unicode_name(aw: Web, name: str) -> None:
    r = await aw.post("/users/new", {"mode": "single", "name": name, "term": "default"})
    assert r.status_code == 200, r.text[:500]
    assert [u.name for u in aw.ctx.db.call(repo.all_users)] == [name]


@pytest.mark.parametrize("name", NAMES)
async def test_rename_to_unicode_name(aw: Web, name: str) -> None:
    await aw.post("/users/new", {"mode": "single", "name": "tmp", "term": "default"})
    r = await aw.post("/users/1/meta", {"name": name, "comment": "", "tg_username": ""})
    assert r.status_code in (200, 303), r.text[:500]
    assert aw.ctx.db.call(repo.all_users)[0].name == name
