# ruff: noqa: RUF001
from __future__ import annotations

import json
import re
from collections.abc import Callable

from tests.web.conftest import PASSWORD, ROOT, Web
from tgpanel.db import repo
from tgpanel.domain.models import CarrierMode, UserStatus
from tgpanel.services.dashboard import DashboardService
from tgpanel.services.requests import RequestService
from tgpanel.services.traffic import TrafficService
from tgpanel.web.adapters import RequestsAdapter, TrafficAdapter


async def flash_text(w: Web, r) -> str:  # type: ignore[no-untyped-def]
    assert r.status_code == 303, r.text[:300]
    page = await w.client.get(r.headers["location"])
    m = re.search(r'<div class="alert (?:ok|err)" role="status">([^<]*)</div>', page.text)
    return m.group(1) if m else ""


async def test_dashboard_with_port_and_degraded(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/"))).text
    assert "2400" in html and "1.0 КБ" in html and "tproxy-server" in html
    aw.traffic.fail = True
    html = (await aw.client.get(aw.u("/"))).text
    assert "Подробная статистика недоступна" in html


async def test_banner_shows_last_failure_and_applying(aw: Web) -> None:
    aw.fake.fail_on("systemctl", "restart tproxy-server")
    await aw.ctx.users.create(
        [__import__("tgpanel.services.api", fromlist=["x"]).NewUser(name="a")], "t"
    )
    html = (await aw.client.get(aw.u("/users"))).text
    assert "завершилось сбоем" in html
    assert "завершилось сбоем" in (await aw.client.get(aw.u("/apply/status"))).text


async def test_user_card_actions(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    base = f"/users/{uid}"
    card = await aw.client.get(aw.u(base))
    assert card.status_code == 200 and 'id="traffic-chart"' in card.text

    r = await aw.post(
        base + "/meta",
        {"name": "alice2", "tg_id": "555", "tg_username": "@alice_x", "comment": "заметка\nвторая"},
    )
    assert "Сохранено" in await flash_text(aw, r)
    user = await aw.ctx.users.get(uid)
    assert (
        user and user.name == "alice2" and user.tg_id == 555 and user.comment == "заметка\nвторая"
    )
    extra = aw.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.tg_username == "alice_x"

    await aw.post(base + "/action", {"action": "disable"})
    assert (await aw.ctx.users.get(uid)).status is UserStatus.DISABLED  # type: ignore[union-attr]
    await aw.post(base + "/action", {"action": "enable"})
    await aw.post(base + "/action", {"action": "extend", "days": "5"})
    await aw.post(base + "/action", {"action": "set_expiry", "expires_date": "2031-05-05"})
    assert (await aw.ctx.users.get(uid)).expires_at.year == 2031  # type: ignore[union-attr]
    await aw.post(base + "/action", {"action": "clear_expiry"})
    assert (await aw.ctx.users.get(uid)).expires_at is None  # type: ignore[union-attr]
    await aw.post(base + "/action", {"action": "carrier", "carrier": "websocket"})
    assert (await aw.ctx.users.get(uid)).carrier_mode is CarrierMode.WEBSOCKET  # type: ignore[union-attr]
    await aw.post(base + "/action", {"action": "carrier", "carrier": ""})
    assert (await aw.ctx.users.get(uid)).carrier_mode is None  # type: ignore[union-attr]

    old = (await aw.ctx.users.get(uid)).secret  # type: ignore[union-attr]
    assert "Подтвердите" in await flash_text(
        aw, await aw.post(base + "/action", {"action": "reissue"})
    )
    await aw.post(base + "/action", {"action": "reissue", "confirm": "1"})
    assert (await aw.ctx.users.get(uid)).secret != old  # type: ignore[union-attr]

    r = await aw.post(base + "/action", {"action": "send_link"})
    assert "отправлена" in await flash_text(aw, r) and aw.broadcast.sent_links == [[uid]]

    bad = await aw.post(base + "/action", {"action": "delete", "confirm_name": "nope"})
    assert "неверно" in await flash_text(aw, bad)
    assert await aw.ctx.users.get(uid) is not None
    ok = await aw.post(base + "/action", {"action": "delete", "confirm_name": "alice2"})
    assert ok.headers["location"] == ROOT + "/users"
    assert await aw.ctx.users.get(uid) is None
    assert (await aw.post(base + "/action", {"action": "bogus"})).status_code in (400, 404)


async def test_user_event_log_on_card(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    await aw.post(f"/users/{uid}/action", {"action": "disable"})
    html = (await aw.client.get(aw.u(f"/users/{uid}"))).text
    assert "user.disable" in html and "user.create" in html


async def test_traffic_json_presets_and_errors(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    for preset in ("24h", "7d", "30d", "all"):
        r = await aw.client.get(aw.u(f"/users/{uid}/traffic.json?preset={preset}"))
        data = r.json()
        assert r.status_code == 200 and data["granularity"] == "hour"
        assert data["total_up"] == 15 and len(data["points"]) == 2 and len(data["points"][0]) == 3
    extra = aw.ctx.db.call(repo.get_user_extra, uid)
    assert extra and aw.traffic.calls[-1][1] == extra.created_at  # "all" starts at creation
    assert (await aw.client.get(aw.u(f"/users/{uid}/traffic.json?preset=bad"))).status_code == 400
    assert (await aw.client.get(aw.u("/users/99/traffic.json"))).status_code == 404
    aw.traffic.fail = True
    r = await aw.client.get(aw.u(f"/users/{uid}/traffic.json"))
    assert r.status_code == 503 and "error" in r.json()


async def test_qr_is_svg_and_has_no_inline_style(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    html = (await aw.post(f"/users/{uid}/reveal")).text
    svg = re.search(r"<svg.*?</svg>", html, re.S)
    assert svg and " style=" not in svg.group(0)
    assert "<img" not in html


async def test_real_traffic_adapter_and_requests_adapter(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    aw.ctx.db.call(repo.add_traffic, "day", uid, aw.clock.now, bytes_up=100, bytes_down=50)
    traffic = TrafficService(aw.ctx.db, aw.clock)
    aw.web.traffic = TrafficAdapter(
        traffic, DashboardService(traffic, aw.ctx.pipeline.ops, aw.ctx.db)
    )
    r = await aw.client.get(aw.u(f"/users/{uid}/traffic.json?preset=7d"))
    assert r.status_code == 200 and r.json()["total_up"] == 100
    assert r.json()["granularity"] in ("hour", "day", "minute")
    assert (await aw.client.get(aw.u(f"/users/{uid}/traffic.json?preset=all"))).status_code == 200
    assert (await aw.client.get(aw.u("/"))).status_code == 200

    svc = RequestService(aw.ctx.pipeline, aw.ctx.db, aw.ctx.users)
    aw.web.requests = RequestsAdapter(svc)
    rid = aw.ctx.db.call(repo.create_access_request, 4242, "bobby", "Bob B", aw.clock.now)
    assert "Bob B" in (await aw.client.get(aw.u("/requests"))).text
    r = await aw.post(f"/requests/{rid}/approve", {"term": "1m"})
    assert "одобрена" in await flash_text(aw, r)
    assert aw.ctx.db.call(repo.get_user_by_tg_id, 4242) is not None
    rid2 = aw.ctx.db.call(repo.create_access_request, 4343, None, "Zed", aw.clock.now)
    assert "отклонена" in await flash_text(aw, await aw.post(f"/requests/{rid2}/reject"))


async def test_requests_with_fake_port(aw: Web) -> None:
    rid = aw.ctx.db.call(repo.create_access_request, 1, "u", "Name <b>x</b>", aw.clock.now)
    html = (await aw.client.get(aw.u("/requests"))).text
    assert "Name &lt;b&gt;x&lt;/b&gt;" in html
    await aw.post(f"/requests/{rid}/approve", {"term": "default"})
    await aw.post(f"/requests/{rid}/reject")
    assert aw.requests.calls == [("approve", rid, None), ("reject", rid, None)]
    assert (await aw.post(f"/requests/{rid}/approve", {"term": "date"})).status_code == 400


async def test_broadcast_flow(aw: Web) -> None:
    ids = await aw.create_users("a", "b")
    for i in ids:
        aw.ctx.db.call(repo.update_user, i, bot_started=True)
    r = await aw.post("/broadcast/preview", {"audience": "active", "template": "Привет {name}"})
    prev_text = r.text
    assert r.status_code == 200 and "Пример: Привет" in r.text and "Выбрано: 2" in r.text
    assert (
        await aw.post("/broadcast/preview", {"audience": "x", "template": "t"})
    ).status_code == 422
    token = re.search(r'name="form_token" value="([^"]+)"', prev_text)
    assert token
    form = {"audience": "all", "template": "Привет", "form_token": token.group(1)}
    r = await aw.post("/broadcast/start", form)
    assert r.headers["location"] == ROOT + "/broadcast/7"
    assert aw.broadcast.started == [("Привет", ids)]
    # the token is one-time: a repeated submit does not send a second broadcast
    again = await aw.post("/broadcast/start", form)
    assert "Форма устарела" in await flash_text(aw, again)
    assert len(aw.broadcast.started) == 1
    no_token = await aw.post("/broadcast/start", {"audience": "all", "template": "x"})
    assert "Форма устарела" in await flash_text(aw, no_token)
    rep = await aw.client.get(aw.u("/broadcast/7"))
    assert rep.status_code == 200 and "отправлено: 2" in rep.text
    assert (await aw.client.get(aw.u("/broadcast/8"))).status_code == 404
    assert (await aw.client.get(aw.u("/broadcast/7/report"))).status_code == 200


async def test_settings_validation_errors_and_save(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/settings"))).text
    for key in (
        "secrets_per_process",
        "default_term",
        "timezone",
        "issuance_mode",
        "backup_keep_last",
    ):
        assert f'name="{key}"' in html
    r = await aw.post(
        "/settings", {"secrets_per_process": "99", "timezone": "Mars/Base", "default_term": "1m"}
    )
    assert r.status_code == 422
    assert "допустимо от 1 до 16" in r.text and "Неизвестный часовой пояс" in r.text
    assert 'value="99"' in r.text  # entered values are kept
    r = await aw.post("/settings", {"timezone": "Europe/Moscow", "backup_keep_last": "7"})
    assert r.status_code == 303
    cfg = await aw.ctx.settings.snapshot()
    assert cfg.timezone == "Europe/Moscow" and cfg.backup_keep_last == 7


async def test_settings_unchanged_does_not_apply(aw: Web) -> None:
    runs = len(aw.ctx.db.call(repo.list_apply_runs, 100))
    cfg = await aw.ctx.settings.all()
    r = await aw.post("/settings", {k: str(v) for k, v in cfg.items()})
    assert "Ничего не изменилось" in await flash_text(aw, r)
    assert len(aw.ctx.db.call(repo.list_apply_runs, 100)) == runs


async def test_settings_proxy_setting_applies_and_rejection_is_shown(aw: Web) -> None:
    await aw.create_users("a", "b", "c")
    r = await aw.post("/settings", {"secrets_per_process": "2"})
    assert "Нельзя поставить" in await flash_text(aw, r)
    r = await aw.post("/settings", {"mtp_workers": "2"})
    assert "Сохранено" in await flash_text(aw, r)


async def test_admins_and_bot_token_are_write_only(aw: Web) -> None:
    await aw.post("/settings/admins", {"action": "add", "tg_id": "777"})
    assert aw.ctx.db.call(repo.list_admins) == [777]
    assert "777" in (await aw.client.get(aw.u("/settings"))).text
    await aw.post("/settings/admins", {"action": "remove", "tg_id": "777"})
    assert aw.ctx.db.call(repo.list_admins) == []
    token = "123456789:" + "A" * 35
    assert "неверный формат" in await flash_text(
        aw, await aw.post("/settings/bot-token", {"token": "bad"})
    )
    assert aw.env_writes == []
    await aw.post("/settings/bot-token", {"token": token})
    assert aw.env_writes == [("TGPANEL_BOT_TOKEN", token)]
    assert aw.ctx.db.call(repo.get_setting, "bot_token") is None  # never stored in the DB
    stored = "\n".join(f"{k}={v}" for k, v in aw.ctx.db.call(repo.all_settings).items())
    assert token not in stored
    html = (await aw.client.get(aw.u("/settings"))).text
    assert token not in html and "Токен задан" in html
    audit = "\n".join(a.details for a in aw.ctx.db.call(repo.list_audit, limit=100))
    assert token not in audit


async def test_message_templates(aw: Web) -> None:
    r = await aw.post("/settings/templates", {"msg.link": "Ваша ссылка: {link}", "msg.welcome": ""})
    assert "Сохранено" in await flash_text(aw, r)
    assert aw.ctx.db.call(repo.get_setting, "msg.link") == "Ваша ссылка: {link}"
    assert "Ваша ссылка: {link}" in (await aw.client.get(aw.u("/settings"))).text
    bad = await aw.post("/settings/templates", {"msg.link": "a" * 32})
    assert "содержит секрет" in await flash_text(aw, bad)


async def test_backups_create_download_restore(aw: Web, _log_capture: Callable[[], str]) -> None:
    (uid,) = await aw.create_users("alice")
    assert "Бэкап создан" in await flash_text(aw, await aw.post("/backups/create"))
    items = aw.ctx.db.call(repo.list_backups)
    manual = next(b for b in items if b.reason == "manual")
    page = (await aw.client.get(aw.u("/backups"))).text
    assert f"/backups/{manual.id}/download" in page
    dl = await aw.post(f"/backups/{manual.id}/download", {"password": PASSWORD})
    assert dl.status_code == 200 and dl.content == aw.fake.files[manual.path].data
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["cache-control"] == "no-store"
    assert (await aw.post("/backups/999/download", {"password": PASSWORD})).status_code == 404
    # restore needs the confirmation word
    r = await aw.post(f"/backups/{manual.id}/restore", {"confirm": "no", "password": PASSWORD})
    assert "введите слово" in await flash_text(aw, r)
    await aw.post("/users/bulk", {"action": "delete", "confirm": "удалить", "ids": [str(uid)]})
    assert aw.ctx.db.call(repo.all_users) == []
    r = await aw.post(
        f"/backups/{manual.id}/restore", {"confirm": "восстановить", "password": PASSWORD}
    )
    # the restore invalidates every session (the pipeline bumps panel_session_version)
    assert r.status_code == 303
    assert (await aw.client.get(aw.u("/backups"))).status_code == 303
    aw.client.cookies.clear()
    assert (await aw.login()).status_code == 303
    assert [u.name for u in aw.ctx.db.call(repo.all_users)] == ["alice"]
    # the download is gone for anonymous clients
    aw.client.cookies.clear()
    assert (
        await aw.client.post(aw.u(f"/backups/{manual.id}/download"), data={"password": PASSWORD})
    ).status_code == 303


async def test_backup_download_refuses_paths_outside_backup_dir(aw: Web) -> None:
    aw.ctx.db.call(repo.add_backup, "/etc/shadow", aw.clock.now, "x", 1)
    rec = aw.ctx.db.call(repo.list_backups)[0]
    assert (await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})).status_code == 404


async def test_audit_and_apply_history_pages(aw: Web) -> None:
    await aw.create_users("alice")
    html = (await aw.client.get(aw.u("/audit"))).text
    assert ("user.create" in html and "web:" in html) or "system" in html
    html = (await aw.client.get(aw.u("/audit?tab=apply"))).text
    assert "create" in html
    assert (await aw.client.get(aw.u("/audit?tab=x"))).status_code == 400
    assert (await aw.client.get(aw.u("/audit?page=zero"))).status_code == 400


async def test_no_secrets_in_logs(aw: Web, _log_capture: Callable[[], str]) -> None:
    ids = await aw.create_users("alice")
    await aw.post(f"/users/{ids[0]}/reveal")
    await aw.post("/users/reveal-many", {"ids": [str(ids[0])]})
    await aw.client.get(aw.u(f"/users/{ids[0]}/traffic.json"))
    await aw.post(f"/users/{ids[0]}/action", {"action": "reissue", "confirm": "1"})
    aw.traffic.fail = True
    await aw.client.get(aw.u("/"))
    aw.assert_no_secret(_log_capture())
    assert "t.me/webproxy" not in _log_capture()
    audit = "\n".join(f"{a.details}{a.target}" for a in aw.ctx.db.call(repo.list_audit, limit=500))
    aw.assert_no_secret(audit)
    assert json.loads('"ok"') == "ok"
