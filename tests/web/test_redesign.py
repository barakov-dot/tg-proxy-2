# ruff: noqa: RUF001
"""UI redesign: dashboard, metrics, global chart, live user filters, icons, accessibility."""

from __future__ import annotations

import re
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.web.conftest import ROOT, Web
from tgpanel.db import repo
from tgpanel.domain.counters import Counter

WEB_DIR = Path(__file__).resolve().parents[2] / "tgpanel" / "web"
PAGES = [
    "/", "/users", "/users/1", "/users/new", "/import", "/requests", "/broadcast",
    "/settings", "/backups", "/audit", "/audit?tab=apply", "/metrics/fragment",
]  # fmt: skip


def make_online(w: Web, ids: list[int]) -> None:
    for uid in ids:
        w.ctx.db.call(
            repo.put_counter_state,
            repo.CounterStateRow(
                uid, Counter(0, 0), Counter(0, 0), True, True, w.clock.now + timedelta(hours=1)
            ),
        )
        w.ctx.db.call(repo.update_user, uid, last_seen_at=w.clock.now)


# ------------------------------------------------------------------------------ dashboard


async def test_dashboard_has_chart_online_list_and_no_apply_history(aw: Web) -> None:
    ids = await aw.create_users("alice", "bob", "carol")
    make_online(aw, ids[:2])
    aw.ctx.db.call(repo.update_user, ids[0], tg_username="alice_tg")
    html = (await aw.client.get(aw.u("/"))).text
    assert "Последние применения" not in html
    assert f'data-url="{ROOT}/traffic/global.json"' in html and 'id="global-chart"' in html
    for preset in ("1d", "7d", "30d"):
        assert f'data-preset="{preset}"' in html
    assert f'href="{ROOT}/users/{ids[0]}">alice</a>' in html
    assert f'href="{ROOT}/users/{ids[1]}">bob</a>' in html
    assert f'href="{ROOT}/users/{ids[2]}">carol</a>' not in html  # offline
    assert "@alice_tg" in html
    assert 'id="metrics"' in html and f"{ROOT}/metrics/fragment" in html
    assert "echarts.min.js" in html


async def test_online_list_is_limited_with_link_to_all(aw: Web) -> None:
    from tgpanel.services.api import NewUser

    res = await aw.ctx.users.create([NewUser(name=f"u{i:02d}") for i in range(25)], "t")
    make_online(aw, list(res.user_ids))
    html = (await aw.client.get(aw.u("/"))).text
    assert html.count('class="dot on"') == 20
    assert f'href="{ROOT}/users?online=1"' in html and "(25)" in html


async def test_dashboard_banner_for_failed_apply_still_present(aw: Web) -> None:
    from tgpanel.services.api import NewUser

    aw.fake.fail_on("systemctl", "restart tproxy-server")
    await aw.ctx.users.create([NewUser(name="x")], "t")
    html = (await aw.client.get(aw.u("/"))).text
    assert "завершилось сбоем" in html and "Последние применения" not in html


async def test_dashboard_still_renders_when_ports_fail(aw: Web) -> None:
    aw.traffic.fail = True
    aw.metrics.fail = True
    assert (await aw.client.get(aw.u("/"))).status_code == 200
    frag = await aw.client.get(aw.u("/metrics/fragment"))
    assert frag.status_code == 200 and "Нет данных" in frag.text


# ------------------------------------------------------------------------------- metrics


async def test_metrics_fragment_renders_values(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/metrics/fragment"))).text
    assert "37%" in html and "25%" in html  # cpu, memory
    assert "2.0 ГБ / 8.0 ГБ" in html and "40.0 ГБ / 100.0 ГБ" in html
    assert "12.5 Мбит/с" in html and "3.2 Мбит/с" in html and "eth0" in html
    assert "1 д 1 ч" in html and "<progress" in html


async def test_metrics_fragment_requires_auth(w: Web) -> None:
    r = await w.client.get(w.u("/metrics/fragment"))
    assert r.status_code == 303 and r.headers["location"] == ROOT + "/login"
    r = await w.client.get(w.u("/metrics/fragment"), headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == ROOT + "/login"


async def test_metrics_without_port_says_no_data(aw: Web) -> None:
    aw.web.metrics = None
    assert "Нет данных" in (await aw.client.get(aw.u("/metrics/fragment"))).text


async def test_metrics_polling_is_htmx_and_pauses_when_hidden(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/"))).text
    assert 'hx-trigger="load, every 5s, tgp-refresh"' in html and "data-poll" in html
    js = (WEB_DIR / "static" / "app.js").read_text()
    assert "document.hidden" in js or "doc.hidden" in js


# ------------------------------------------------------------------------ global chart


async def test_global_chart_json_presets(aw: Web) -> None:
    for preset, hours in (("1d", 24), ("7d", 168), ("30d", 720)):
        r = await aw.client.get(aw.u(f"/traffic/global.json?preset={preset}"))
        data = r.json()
        assert r.status_code == 200 and data["granularity"] == "hour"
        assert data["total_up"] == 150 and data["total_down"] == 260 and len(data["points"]) == 2
        start, end = aw.traffic.global_calls[-1]
        assert end - start == timedelta(hours=hours)
    assert (await aw.client.get(aw.u("/traffic/global.json"))).status_code == 200  # default 1d
    assert (await aw.client.get(aw.u("/traffic/global.json?preset=all"))).status_code == 400
    aw.traffic.fail = True
    r = await aw.client.get(aw.u("/traffic/global.json"))
    assert r.status_code == 503 and "error" in r.json()


async def test_global_chart_requires_auth(w: Web) -> None:
    assert (await w.client.get(w.u("/traffic/global.json"))).status_code == 303


# ------------------------------------------------------------------------ live filters


async def seed_users(aw: Web) -> None:
    await aw.create_users("alice", "bob", "carol")
    aw.ctx.db.call(repo.update_user, 2, comment="vip")


def names_of(html: str) -> list[str]:
    found = re.findall(r'<a href="[^"]*/users/\d+">([^<]+)</a>', html)
    return [n for n in found if n != "открыть"]


async def test_users_returns_fragment_for_htmx_and_full_page_otherwise(aw: Web) -> None:
    await seed_users(aw)
    hx = await aw.client.get(aw.u("/users?q=bob"), headers={"HX-Request": "true"})
    assert hx.status_code == 200 and "<html" not in hx.text and "<head" not in hx.text
    assert 'id="users-region"' in hx.text and names_of(hx.text) == ["bob"]
    assert "Vary" in hx.headers and "HX-Request" in hx.headers["vary"]
    full = await aw.client.get(aw.u("/users?q=bob"))
    assert (
        "<html" in full.text and 'id="users-region"' in full.text and names_of(full.text) == ["bob"]
    )
    restore = await aw.client.get(
        aw.u("/users?q=bob"), headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    assert "<html" in restore.text  # browser back/forward needs the whole page


async def test_live_filter_form_wiring(aw: Web) -> None:
    await seed_users(aw)
    html = (await aw.client.get(aw.u("/users"))).text
    form = re.search(r'<form id="filters"[^>]*>', html)
    assert form
    tag = form.group(0)
    assert f'hx-get="{ROOT}/users"' in tag and 'hx-target="#users-region"' in tag
    assert "input changed delay:250ms, change" in tag and 'hx-push-url="true"' in tag
    assert "hx-sync" in tag
    assert 'name="q"' in html and 'type="search"' in html
    # sort and pagination links work with and without JavaScript
    assert re.search(r'<a href="[^"]*sort=name[^"]*"\s+hx-get="[^"]*sort=name', html)
    # sort / direction are kept by the form through form-associated hidden inputs
    assert 'name="sort" value="id" form="filters"' in html and 'name="dir"' in html
    assert 'name="expected_total" value="3"' in html  # bulk protection survives re-render


async def test_live_filters_each_control_through_fragment(aw: Web) -> None:
    await seed_users(aw)
    aw.ctx.db.call(repo.update_user, 3, tg_id=1003)
    hx = {"HX-Request": "true"}
    cases = {
        "q=al": ["alice"],
        "comment=vip": ["bob"],
        "tg=1": ["carol"],
        "status=active&status=expired": ["alice", "bob", "carol"],
        "sort=name&dir=desc": ["carol", "bob", "alice"],
        "per=100&period=24h": ["alice", "bob", "carol"],
    }
    for query, expected in cases.items():
        r = await aw.client.get(aw.u("/users?" + query), headers=hx)
        assert r.status_code == 200, query
        assert names_of(r.text) == expected, query
    bad = await aw.client.get(aw.u("/users?sort=nope"), headers=hx)
    assert bad.status_code == 400 and "<html" not in bad.text  # whitelist still enforced


async def test_selection_script_keeps_visible_rows_checked() -> None:
    js = (WEB_DIR / "static" / "app.js").read_text()
    assert "htmx:afterSwap" in js and "restoreSelection" in js and "selected" in js


async def test_all_matching_needs_expected_total_in_fragment(aw: Web) -> None:
    await seed_users(aw)
    frag = (await aw.client.get(aw.u("/users?q=a"), headers={"HX-Request": "true"})).text
    assert 'name="expected_total" value="2"' in frag  # alice, carol
    assert 'name="filter_qs" value="q=a"' in frag


# ----------------------------------------------------------------------- icons and CSS


def sprite_ids() -> set[str]:
    text = (WEB_DIR / "static" / "icons.svg").read_text()
    return set(re.findall(r'<symbol id="(i-[a-z0-9-]+)"', text))


async def test_icon_sprite_is_served(w: Web) -> None:
    r = await w.client.get(w.u("/static/icons.svg"))
    assert r.status_code == 200 and "svg" in r.headers["content-type"]
    assert r.text.count("<symbol") >= 40


def test_every_icon_used_in_templates_exists_in_sprite() -> None:
    known = sprite_ids()
    used: set[str] = set()
    for path in (WEB_DIR / "templates").glob("*.html"):
        text = path.read_text()
        used |= {f"i-{n}" for n in re.findall(r"icon\('([a-z0-9-]+)'", text)}
        used |= {f"i-{n}" for n in re.findall(r"icon\(\"([a-z0-9-]+)\"", text)}
        used |= {f"i-{n}" for n in re.findall(r"\('[a-z]+','[^']*','([a-z0-9-]+)'\)", text)}
        used |= set(re.findall(r"icons\.svg'\) }}#(i-[a-z0-9-]+)", text))
    assert used, "no icons found in templates"
    assert used <= known, sorted(used - known)
    for needed in ("dashboard", "users", "user-plus", "import", "inbox", "megaphone", "settings",
                   "archive", "list", "link", "copy", "eye", "qr", "trash", "power", "refresh",
                   "clock", "calendar", "search", "filter", "download", "chevron-left",
                   "chevron-right", "chevron-down", "sun", "moon", "cpu", "memory", "disk",
                   "arrow-up", "arrow-down", "activity", "check", "alert", "wifi"):  # fmt: skip
        assert f"i-{needed}" in known, needed


async def test_rendered_icon_references_resolve(aw: Web) -> None:
    known = sprite_ids()
    await aw.create_users("alice")
    for path in PAGES:
        html = (await aw.client.get(aw.u(path))).text
        refs = set(re.findall(r'icons\.svg#(i-[a-z0-9-]+)"', html))
        assert refs <= known, (path, sorted(refs - known))
        if path not in ("/metrics/fragment",):
            assert refs, path


# ----------------------------------------------------------------------------- CSP / a11y


async def test_no_inline_script_style_or_handlers_on_new_pages(aw: Web) -> None:
    await aw.create_users("alice")
    for path in [*PAGES, "/login"]:
        client = aw.client
        r = await client.get(aw.u(path))
        html = r.text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
        assert " style=" not in html and "<style" not in html, path
        assert not re.search(r"\son[a-z]+=", html), path
        assert "'unsafe-inline'" not in r.headers["content-security-policy"], path
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert not src.startswith(("http://", "https://", "//")), (path, src)
    css = (WEB_DIR / "static" / "app.css").read_text()
    assert "@import" not in css and "http" not in css and "url(" not in css


async def test_theme_toggle_and_sync_theme_script(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/"))).text
    assert "data-theme-toggle" in html and 'aria-label="Переключить светлую и тёмную тему"' in html
    theme_tag = re.search(r'<script src="[^"]*theme\.js"[^>]*>', html)
    assert theme_tag and "defer" not in theme_tag.group(0)  # runs before the first paint
    js = (WEB_DIR / "static" / "theme.js").read_text()
    assert "try" in js and "localStorage" in js
    css = (WEB_DIR / "static" / "app.css").read_text()
    assert "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css


class A11y(HTMLParser):
    """Collects controls without an accessible name."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None], list[str]]] = []
        self.labels: list[int] = []
        self.problems: list[str] = []
        self.label_depth = 0
        self.ids_with_label: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.ids_with_label.add(a["for"] or "")
        if tag in ("button", "a"):
            self.stack.append((tag, a, []))
        if tag in ("input", "select", "textarea"):
            kind = (a.get("type") or "text").lower()
            if kind in ("hidden", "submit") or "hidden" in a:
                return
            named = a.get("aria-label") or a.get("title") or a.get("placeholder")
            if not (self.label_depth or named or a.get("id") in self.ids_with_label):
                self.problems.append(f"{tag}[{a.get('name')}] has no label")
        if tag == "use" and self.stack:
            self.stack[-1][2].append("")  # the icon itself carries no text
        if tag == "progress" and not a.get("aria-label"):
            self.problems.append("progress without aria-label")

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        if tag in ("button", "a") and self.stack and self.stack[-1][0] == tag:
            _, a, texts = self.stack.pop()
            text = "".join(texts).strip()
            if not text and not (a.get("aria-label") or a.get("title")):
                self.problems.append(f"{tag} without accessible name: {a}")

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1][2].append(data)


@pytest.mark.parametrize("path", PAGES)
async def test_accessibility_basics(aw: Web, path: str) -> None:
    await aw.create_users("alice")
    html = (await aw.client.get(aw.u(path))).text
    parser = A11y()
    parser.feed(html)
    assert parser.problems == [], parser.problems
    if "<html" in html:
        assert '<html lang="ru">' in html
        assert 'aria-hidden="true"' in html  # decorative icons are hidden from screen readers


async def test_login_page_redesigned_and_accessible(w: Web) -> None:
    html = (await w.client.get(w.u("/login"))).text
    assert "login-card" in html and "icons.svg#i-shield" in html and "sidebar" not in html
    parser = A11y()
    parser.feed(html)
    assert parser.problems == []
    assert '<html lang="ru">' in html


async def test_sidebar_navigation_and_mobile_toggle(aw: Web) -> None:
    html = (await aw.client.get(aw.u("/users"))).text
    assert 'class="sidebar"' in html and 'aria-current="page"' in html
    assert "data-nav-toggle" in html and 'aria-controls="sidebar"' in html
    for path in ("/", "/users", "/users/new", "/import", "/requests", "/broadcast", "/settings",
                 "/backups", "/audit"):  # fmt: skip
        assert f'href="{ROOT}{path}"' in html
    css = (WEB_DIR / "static" / "app.css").read_text()
    assert "max-width: 900px" in css and "nav-open" in css
    assert "max-width: 1" not in css.split(".content")[1][:200]  # no narrow centred container
