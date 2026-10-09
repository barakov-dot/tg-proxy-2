# ruff: noqa: RUF001
"""(d) bot flows, (e) expiry and extension, (f) failure injection with all its signals."""

from __future__ import annotations

from datetime import timedelta

from tests.e2e.conftest import ADMIN, E2E, USER
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import NewUser


async def test_bot_request_admin_approve_sends_link_after_apply(e2e: E2E) -> None:
    e2e.set_setting("issuance_mode", "approval")
    await e2e.tg.send(USER, "/start")
    assert "req" in e2e.session.button_data(USER)
    runs = len(e2e.successful_runs())
    await e2e.tg.press(USER, "req")
    assert e2e.session.button_data(ADMIN) == ["ra:1", "rr:1"]
    assert len(e2e.successful_runs()) == runs  # a request alone changes nothing
    assert not any("t.me/webproxy" in t for t in e2e.session.texts(USER))
    await e2e.tg.press(ADMIN, "ra:1")
    await e2e.tg.press(ADMIN, "rt:1:1m")
    assert len(e2e.successful_runs()) == runs + 1
    user = e2e.stack.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user is not None and user.status is UserStatus.ACTIVE
    assert any(e2e.link_html(user) in t for t in e2e.session.texts(USER))
    assert f"u{user.id}" in [p["name"] for p in e2e.profiles()]
    assert e2e.readyz_calls() == 1


async def test_web_approves_the_same_request_service(e2e: E2E) -> None:
    e2e.set_setting("issuance_mode", "approval")
    await e2e.tg.press(USER, "req")
    assert (await e2e.login()).status_code == 303
    page = await e2e.client.get(e2e.u("/requests"))
    assert page.status_code == 200
    res = await e2e.post("/requests/1/approve", {"term": "1m"})
    assert res.status_code == 303
    user = e2e.stack.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user is not None
    assert any(e2e.link_html(user) in t for t in e2e.session.texts(USER))  # delivered by the bot


async def test_open_mode_issues_without_admin(e2e: E2E) -> None:
    e2e.set_setting("issuance_mode", "open")
    e2e.set_setting("open_mode_batch_window_s", "0")
    runs = len(e2e.successful_runs())
    await e2e.tg.press(USER, "req")
    user = e2e.stack.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user is not None and len(e2e.successful_runs()) == runs + 1
    assert any(e2e.link_html(user) in t for t in e2e.session.texts(USER))
    assert e2e.session.button_data(ADMIN) == []


async def test_expiry_disables_with_one_apply_notifies_and_extension_reenables(e2e: E2E) -> None:
    users = e2e.stack.ctx.users
    now = e2e.clock.now
    res = await users.create(
        [
            NewUser("Скоро", tg_id=USER, expires_at=now + timedelta(days=1)),
            NewUser("Тоже", expires_at=now + timedelta(days=1)),
            NewUser("Вечный"),
        ],
        "web:admin",
    )
    assert res.ok, res.error
    e2e.db(repo.update_user, res.user_ids[0], bot_started=True, can_message=True)
    runs = len(e2e.successful_runs())
    e2e.clock.now += timedelta(days=2)
    await e2e.stack.runtime.scheduler.tick()
    assert len(e2e.successful_runs()) == runs + 1  # ONE apply for both
    statuses = {u.name: u.status for u in e2e.users()}
    assert statuses == {
        "Скоро": UserStatus.EXPIRED,
        "Тоже": UserStatus.EXPIRED,
        "Вечный": UserStatus.ACTIVE,
    }
    names = [p["name"] for p in e2e.profiles()]
    assert f"u{res.user_ids[0]}" not in names and f"u{res.user_ids[2]}" in names
    assert any("срок" in t.lower() for t in e2e.session.texts(USER))  # the notice
    await e2e.stack.runtime.scheduler.tick()  # idempotent: nothing more to do
    assert len(e2e.successful_runs()) == runs + 1
    # extension through the web re-enables
    assert (await e2e.login()).status_code == 303
    res_ext = await e2e.post(f"/users/{res.user_ids[0]}/action", {"action": "extend", "days": "30"})
    assert res_ext.status_code == 303
    assert e2e.user_by_name("Скоро").status is UserStatus.ACTIVE
    assert len(e2e.successful_runs()) == runs + 2
    assert f"u{res.user_ids[0]}" in [p["name"] for p in e2e.profiles()]


def _config_files(e2e: E2E) -> dict[str, tuple[bytes, int, str, str]]:
    return {
        p: (f.data, f.mode, f.owner, f.group)
        for p, f in e2e.fake.files.items()
        if not p.startswith(("/var/backups/", "/var/lib/tgpanel/"))
    }


async def test_failure_at_relay_restart_shows_error_banner_and_alerts_admin(e2e: E2E) -> None:
    assert (await e2e.login()).status_code == 303
    await e2e.create_via_web("Первый")
    before = _config_files(e2e)
    users_before = len(e2e.users())
    runs_before = len(e2e.runs())
    e2e.fake.fail_on("systemctl", "restart tproxy-server")
    res = await e2e.create_via_web("Второй")
    assert res.status_code == 409
    assert "Второй" not in "".join(u.name for u in e2e.users())
    assert len(e2e.users()) == users_before
    assert "t.me/webproxy" not in res.text and "<code>" not in res.text  # no link, no secret
    assert _config_files(e2e) == before  # byte-identical, rolled back
    runs = e2e.runs()
    assert len(runs) == runs_before + 1 and runs[0].status == "failed"
    dash = await e2e.client.get(e2e.u("/"))
    assert 'class="alert err"' in dash.text and f"#{runs[0].id}" in dash.text
    admin_texts = "\n".join(e2e.session.texts(ADMIN))
    assert "Не удалось применить" in admin_texts
    # the system is healthy again afterwards
    again = await e2e.create_via_web("Второй")
    assert again.status_code == 200
    assert e2e.user_by_name("Второй").status is UserStatus.ACTIVE
