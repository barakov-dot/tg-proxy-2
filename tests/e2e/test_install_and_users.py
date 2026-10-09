"""(a) fresh install -> web login -> create user -> traffic; (b) import; (c) declined import."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from tests.e2e.conftest import E2E, PASSWORD
from tgpanel.domain.models import UserStatus

IMPORT_REGEX = r"^user_(\d{5,15})$"
FIXTURE_PROFILES = Path(__file__).parents[1] / "fixtures/upstream/owner/profiles.json"


def hidden_form(html: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html):
        if not name.startswith(("skip_", "ack_")):
            data[name] = value
    return data


async def test_fresh_install_login_create_and_traffic(e2e: E2E) -> None:
    # the installer's first apply passed the legacy "default" profile through untouched
    assert e2e.profiles()[0]["name"] == "default"
    assert (await e2e.client.get(e2e.u("/"))).status_code == 303  # not logged in
    bad = await e2e.login("wrong password value")
    assert bad.status_code == 401
    assert (await e2e.login(PASSWORD)).status_code == 303
    assert (await e2e.client.get(e2e.u("/"))).status_code == 200

    runs_before = len(e2e.successful_runs())
    created = await e2e.create_via_web("Тестовый")
    assert created.status_code == 200
    assert len(e2e.successful_runs()) == runs_before + 1  # ONE apply
    assert "https://t.me/webproxy" not in created.text  # the link only after an explicit reveal
    user = e2e.user_by_name("Тестовый")
    assert user.status is UserStatus.ACTIVE
    assert f"u{user.id}" in [p["name"] for p in e2e.profiles()]
    assert any(p["secret"].endswith(user.secret[-32:]) for p in e2e.profiles())
    revealed = await e2e.reveal(user.id)
    assert e2e.stack.ctx.users.link(user) in revealed.replace("&amp;", "&")
    assert e2e.readyz_calls() == 1  # /readyz only by the apply, once

    # traffic: baseline poll, then fake nft counters grow, then the next poll records it
    await e2e.stack.collector.poll_once(e2e.clock())
    e2e.fake.add_traffic(
        user.loopback_ip, up_bytes=5000, down_bytes=90_000, up_packets=40, down_packets=80
    )
    e2e.clock.now += timedelta(seconds=30)
    result = await e2e.stack.collector.poll_once(e2e.clock())
    assert result.updated == 1
    series = await e2e.client.get(e2e.u(f"/users/{user.id}/traffic.json?preset=24h"))
    body = series.json()
    assert (body["total_up"], body["total_down"]) == (5000, 90_000)
    card = await e2e.client.get(e2e.u(f"/users/{user.id}"))
    assert card.status_code == 200
    view = await e2e.stack.dashboard.view()
    assert view.up_24h == 5000 and view.down_24h == 90_000
    assert e2e.readyz_calls() == 1  # reading traffic never calls /readyz
    # the user list shows the traffic too
    listing = await e2e.client.get(e2e.u("/users"))
    assert listing.status_code == 200 and "Тестовый" in listing.text


async def test_healthz_is_public_and_minimal(e2e: E2E) -> None:
    res = await e2e.client.get(e2e.u("/healthz"))
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert (await e2e.client.get("/healthz")).json() == {"ok": True}


async def test_import_owner_profiles_via_web_keeps_links(owner: E2E) -> None:
    old = {p["name"]: p["secret"] for p in json.loads(FIXTURE_PROFILES.read_text())["profiles"]}
    assert len(old) == 15
    assert (await owner.login()).status_code == 303
    preview = await owner.post("/import/preview", {"regex": IMPORT_REGEX, "csv_text": ""})
    assert preview.status_code == 200 and "Будет импортировано: 15" in preview.text
    form = {k: v for k, v in hidden_form(preview.text).items() if k != "csrf_token"}
    form.update({"csv_text": "", "regex": IMPORT_REGEX, "ack_old_bot": "1"})
    done = await owner.post("/import/confirm", form)
    assert done.status_code == 303
    users = owner.users()
    assert len(users) == 15 and all(u.imported for u in users)
    for user in users:
        # Telegram IDs were parsed from the profile names; secrets (so the links) are unchanged
        assert user.tg_id is not None and old[f"user_{user.tg_id}"] == user.secret
    new_profile_secrets = sorted(p["secret"] for p in owner.profiles())
    assert new_profile_secrets == sorted(old.values())
    # a new user afterwards: one apply, old links still valid
    before = len(owner.successful_runs())
    created = await owner.create_via_web("Новый")
    assert created.status_code == 200
    assert len(owner.successful_runs()) == before + 1
    secrets_now = {p["secret"] for p in owner.profiles()}
    assert set(old.values()) <= secrets_now and len(secrets_now) == 16
    assert owner.readyz_calls() <= len(owner.successful_runs())


async def test_declined_import_foreign_profiles_survive(owner: E2E) -> None:
    foreign = {p["name"]: p for p in json.loads(FIXTURE_PROFILES.read_text())["profiles"]}
    first = await owner.pipeline.apply_now("install", "system")
    assert first.ok, first.error
    assert owner.users() == []
    assert (await owner.login()).status_code == 303

    def foreign_now() -> dict[str, dict[str, object]]:
        return {p["name"]: p for p in owner.profiles() if p["name"] in foreign}

    assert foreign_now() == foreign
    created = await owner.create_via_web("Свой")
    assert created.status_code == 200
    assert foreign_now() == foreign
    user = owner.user_by_name("Свой")
    assert (await owner.post(f"/users/{user.id}/action", {"action": "disable"})).status_code == 303
    assert owner.user_by_name("Свой").status is UserStatus.DISABLED
    assert foreign_now() == foreign
    assert f"u{user.id}" not in [p["name"] for p in owner.profiles()]
    resp = await owner.post(
        f"/users/{user.id}/action", {"action": "delete", "confirm_name": "Свой"}
    )
    assert resp.status_code == 303
    assert owner.users() == []
    assert foreign_now() == foreign


async def test_global_guard_really_detects_a_leak(e2e: E2E) -> None:
    assert (await e2e.login()).status_code == 303
    await e2e.create_via_web("Утечка")
    secret = e2e.user_by_name("Утечка").secret
    e2e.assert_no_leaks("clean log text")
    for dirty in (f"log {secret} log", "0123456789abcdef0123456789abcdef"):
        try:
            e2e.assert_no_leaks(dirty)
        except AssertionError:
            continue
        raise AssertionError("the guard missed a planted secret")
