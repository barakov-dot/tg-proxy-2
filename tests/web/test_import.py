from __future__ import annotations

import re

from tests.web.conftest import ROOT, Web
from tgpanel.db import repo


async def flash_text(w: Web, r) -> str:  # type: ignore[no-untyped-def]
    assert r.status_code == 303, r.text[:300]
    page = await w.client.get(r.headers["location"])
    m = re.search(r'<div class="alert (?:ok|err)" role="status">([^<]*)</div>', page.text)
    return m.group(1) if m else ""


def hidden_form(html: str) -> dict[str, str]:
    """Form values of the preview page (inputs + the hidden csv textarea)."""
    data: dict[str, str] = {}
    for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html):
        if not name.startswith(("skip_", "ack_")):  # checkboxes are sent only when ticked
            data[name] = value
    return data


async def test_import_page_warns_about_old_bot(owner_web: Web) -> None:
    html = (await owner_web.client.get(owner_web.u("/import"))).text
    assert "Прежний бот должен быть остановлен" in html


async def test_import_preview_edit_skip_confirm(owner_web: Web) -> None:
    w = owner_web
    r = await w.post("/import/preview", {"regex": r"^user_(\d{5,15})$", "csv_text": ""})
    assert (
        r.status_code == 200 and "user_93455874" in r.text and "Будет импортировано: 15" in r.text
    )
    assert "Прежний бот должен быть остановлен" in r.text
    assert w.ctx.db.call(repo.all_users) == []  # preview is read-only
    form = {k: v for k, v in hidden_form(r.text).items() if k != "csrf_token"}
    form["csv_text"] = ""
    form["regex"] = r"^user_(\d{5,15})$"
    form["tg_0"] = "999999"  # edit the Telegram id of row 0
    form["dn_1"] = "Второй"  # edit the display name of row 1
    form["cm_2"] = "заметка"  # edit the comment of row 2
    form["skip_3"] = "1"  # skip row 3
    # no acknowledgement of the old-bot warning -> refused
    refused = await w.post("/import/confirm", form)
    assert "остановлен" in await flash_text(w, refused)
    assert w.ctx.db.call(repo.all_users) == []
    form["ack_old_bot"] = "1"
    done = await w.post("/import/confirm", form)
    assert done.headers["location"] == ROOT + "/users"
    assert "Импортировано: 14, пропущено: 1" in await flash_text(w, done)
    users = w.ctx.db.call(repo.all_users)
    assert len(users) == 14 and all(u.imported for u in users)
    assert users[0].tg_id == 999999
    assert any(u.name == "Второй" for u in users)
    assert any("заметка" in u.comment for u in users), [u.comment for u in users]
    # idempotent: a second import has nothing to do
    again = await w.post("/import/preview", {"regex": r"^user_(\d{5,15})$"})
    assert "Будет импортировано: 1" in again.text  # only the skipped profile remains


async def test_import_bad_regex_and_csv_errors(owner_web: Web) -> None:
    w = owner_web
    r = await w.post("/import/preview", {"regex": r"^(a+)+$"})
    assert r.status_code == 200 and ("regex" in r.text or "id regex" in r.text)
    r = await w.post("/import/preview", {"regex": r"^user_(\d+)$", "csv_text": "x;notanumber;;"})
    assert "invalid telegram id" in r.text
    assert w.ctx.db.call(repo.all_users) == []


async def test_import_recalc_applies_edits_to_preview(owner_web: Web) -> None:
    w = owner_web
    r = await w.post("/import/preview", {"regex": r"^user_(\d{5,15})$"})
    form = {k: v for k, v in hidden_form(r.text).items() if k != "csrf_token"}
    form["csv_text"] = ""
    form["tg_0"] = "55555"
    form["action"] = "recalc"
    r2 = await w.post("/import/preview", form)
    assert r2.status_code == 200 and 'value="55555"' in r2.text


async def test_import_requires_auth(w: Web) -> None:
    r = await w.client.post(w.u("/import/confirm"), data={})
    assert r.status_code == 303
