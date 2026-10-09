from __future__ import annotations

import re
from datetime import timedelta

from tests.web.conftest import PASSWORD, ROOT, Web
from tgpanel.db import repo


async def test_protected_pages_redirect_to_login(w: Web) -> None:
    for path in ("/", "/users", "/settings", "/backups", "/audit", "/import"):
        r = await w.client.get(w.u(path))
        assert r.status_code == 303 and r.headers["location"] == ROOT + "/login"


async def test_htmx_unauthenticated_gets_hx_redirect(w: Web) -> None:
    r = await w.client.get(w.u("/apply/status"), headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == ROOT + "/login"


async def test_login_success_sets_hardened_cookie(w: Web) -> None:
    r = await w.login()
    assert r.status_code == 303 and r.headers["location"] == ROOT + "/"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("tgp_session="))
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low
    assert f"path={ROOT}".lower() in low and "max-age=43200" in low
    assert (await w.client.get(w.u("/"))).status_code == 200


async def test_login_failures_are_generic(w: Web) -> None:
    bad_pw = await w.login(password="nope")
    bad_user = await w.login(username="root")
    assert bad_pw.status_code == bad_user.status_code == 401
    assert "Неверный логин или пароль" in bad_pw.text
    assert bad_pw.text.count("Неверный") == bad_user.text.count("Неверный")


async def test_login_denied_when_credentials_missing(w: Web) -> None:
    w.ctx.db.call(repo.delete_setting, "panel_password_hash")
    assert (await w.login()).status_code == 401
    w.ctx.db.call(repo.delete_setting, "panel_login")
    assert (await w.login()).status_code == 401


async def test_login_requires_login_token(w: Web) -> None:
    await w.client.get(w.u("/login"))
    r = await w.client.post(
        w.u("/login"), data={"username": "admin", "password": PASSWORD, "login_token": "x"}
    )
    assert r.status_code == 400


async def test_lockout_with_growing_backoff(w: Web) -> None:
    for _ in range(5):
        assert (await w.login(password="bad")).status_code == 401
    blocked = await w.login(password="bad")
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 60
    # even the right password is refused while blocked
    assert (await w.login()).status_code == 429
    w.clock.now += timedelta(seconds=61)
    for _ in range(5):
        assert (await w.login(password="bad")).status_code == 401
    second = await w.login(password="bad")
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 120
    w.clock.now += timedelta(seconds=125)
    assert (await w.login()).status_code == 303


async def test_xff_trusted_only_from_proxy(w: Web) -> None:
    # peer 203.0.113.9 is not a trusted proxy: X-Forwarded-For must not give a fresh budget
    for i in range(6):
        await w.client.get(w.u("/login"))
        token = re.search(
            r'name="login_token" value="([^"]*)"', (await w.client.get(w.u("/login"))).text
        )
        assert token
        r = await w.client.post(
            w.u("/login"),
            data={"username": "a", "password": "b", "login_token": token.group(1)},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
    assert r.status_code == 429


async def test_xff_used_from_trusted_proxy(w: Web) -> None:
    import httpx

    from tgpanel.web.app import create_app

    app = create_app(w.web, ROOT)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        for i in range(7):
            page = await c.get(ROOT + "/login")
            token = re.search(r'name="login_token" value="([^"]*)"', page.text)
            assert token
            r = await c.post(
                ROOT + "/login",
                data={"username": "a", "password": "b", "login_token": token.group(1)},
                headers={"X-Forwarded-For": f"198.51.100.{i}"},
            )
            assert r.status_code == 401  # every forwarded client has its own budget


async def test_csrf_rejected_without_or_with_wrong_token(aw: Web) -> None:
    ids = await aw.create_users("alice")
    url = aw.u(f"/users/{ids[0]}/action")
    assert (await aw.client.post(url, data={"action": "disable"})).status_code == 403
    r = await aw.client.post(url, data={"action": "disable", "csrf_token": "wrong"})
    assert r.status_code == 403
    r = await aw.client.post(url, data={"action": "disable"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert (await aw.ctx.users.get(ids[0])).status.value == "active"  # type: ignore[union-attr]
    ok = await aw.client.post(
        url, data={"action": "disable"}, headers={"X-CSRF-Token": await aw.csrf()}
    )
    assert ok.status_code == 303


async def test_logout_clears_session(aw: Web) -> None:
    r = await aw.post("/logout")
    assert r.status_code == 303
    aw.client.cookies.clear()
    assert (await aw.client.get(aw.u("/users"))).status_code == 303


async def test_session_expires_after_12h(aw: Web) -> None:
    assert (await aw.client.get(aw.u("/users"))).status_code == 200
    aw.clock.now += timedelta(hours=12, minutes=1)
    assert (await aw.client.get(aw.u("/users"))).status_code == 303


async def test_session_version_bump_invalidates(aw: Web) -> None:
    aw.ctx.db.call(repo.set_setting, "panel_session_version", "2")
    assert (await aw.client.get(aw.u("/users"))).status_code == 303


async def test_tampered_cookie_rejected(aw: Web) -> None:
    aw.client.cookies.clear()
    aw.client.cookies.set("tgp_session", "garbage", domain="testserver", path=ROOT)
    assert (await aw.client.get(aw.u("/users"))).status_code == 303


async def test_password_change_flow(aw: Web) -> None:
    new = "another long passphrase 42"
    r = await aw.post("/settings/password", {"current": "wrong", "new": new, "again": new})
    assert "Текущий пароль неверен" in (await aw.client.get(aw.u("/settings"))).text
    assert r.status_code == 303
    await aw.post("/settings/password", {"current": PASSWORD, "new": "short", "again": "short"})
    assert "не короче" in (await aw.client.get(aw.u("/settings"))).text
    r = await aw.post("/settings/password", {"current": PASSWORD, "new": new, "again": new})
    assert r.status_code == 303
    assert (await aw.client.get(aw.u("/settings"))).status_code == 200  # session re-issued
    aw.client.cookies.clear()
    assert (await aw.login()).status_code == 401
    assert (await aw.login(password=new)).status_code == 303
    actions = {a.action for a in aw.ctx.db.call(repo.list_audit, limit=100)}
    assert "web.password_change" in actions


async def test_logout_all_invalidates_other_sessions(aw: Web) -> None:
    import httpx

    from tgpanel.web.app import create_app

    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(aw.web, ROOT), client=("198.51.100.1", 1)),
        base_url="https://testserver",
    )
    async with other:
        page = await other.get(ROOT + "/login")
        token = re.search(r'name="login_token" value="([^"]*)"', page.text)
        assert token
        await other.post(
            ROOT + "/login",
            data={"username": "admin", "password": PASSWORD, "login_token": token.group(1)},
        )
        assert (await other.get(ROOT + "/users")).status_code == 200
        await aw.post("/settings/logout-all")
        assert (await other.get(ROOT + "/users")).status_code == 303
