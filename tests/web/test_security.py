from __future__ import annotations

import re

from tests.web.conftest import ROOT, Web


async def test_security_headers_on_public_and_private_pages(aw: Web) -> None:
    for path in ("/login", "/", "/users"):
        r = await aw.client.get(aw.u(path))
        h = r.headers
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp
        assert "style-src 'self'" in csp and "'unsafe-inline'" not in csp
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-frame-options"] == "DENY"
        assert h["cache-control"] == "no-store"


async def test_headers_also_on_errors_and_redirects(aw: Web) -> None:
    for path in ("/users/9999", "/nope"):
        r = await aw.client.get(aw.u(path))
        assert r.status_code == 404 and "content-security-policy" in r.headers


async def test_no_inline_scripts_styles_or_external_urls(aw: Web) -> None:
    await aw.create_users("alice")
    for path in (
        "/",
        "/users",
        "/users/1",
        "/users/new",
        "/import",
        "/requests",
        "/broadcast",
        "/settings",
        "/backups",
        "/audit",
    ):
        r = await aw.client.get(aw.u(path))
        assert r.status_code == 200, path
        html = r.text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
        assert " style=" not in html and "<style" not in html, path
        assert not re.search(r"\son[a-z]+=", html), path
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert not src.startswith(("http://", "https://", "//")), (path, src)


async def test_static_assets_served_under_root_path(w: Web) -> None:
    for name in ("app.css", "app.js", "chart.js", "vendor/htmx.min.js", "vendor/echarts.min.js"):
        r = await w.client.get(w.u("/static/" + name))
        assert r.status_code == 200, name
    login = (await w.client.get(w.u("/login"))).text
    assert f'href="{ROOT}/static/app.css"' in login and f'src="{ROOT}/static/app.js"' in login


async def test_urls_redirects_and_cookie_paths_carry_root_path(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/"))).text
    assert f'href="{ROOT}/users"' in html
    r = await aw.post("/logout")
    assert r.headers["location"] == f"{ROOT}/login"
    r = await aw.client.get(aw.u("/login"))
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("tgp_login="))
    assert f"Path={ROOT}" in cookie


async def test_docs_and_openapi_disabled(w: Web) -> None:
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert (await w.client.get(w.u(path))).status_code == 404
