# ruff: noqa: RUF001
"""Regression tests for the security review (W1-W12)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from tests.web.conftest import PASSWORD, ROOT, SECRET_KEY, Web
from tgpanel.apply.backup import BackupError
from tgpanel.db import repo
from tgpanel.services.backups import BackupService, DownloadBusy
from tgpanel.services.dashboard import DashboardService
from tgpanel.services.traffic import TrafficService
from tgpanel.web.app import create_app
from tgpanel.web.inputs import ip_key, parse_day, parse_decimal, parse_uint, valid_username
from tgpanel.web.security import Signer

WEB_DIR = Path(__file__).resolve().parents[2] / "tgpanel" / "web"


@asynccontextmanager
async def client_from(w: Web, peer: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(w.web, ROOT), client=(peer, 1))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def attempt(c: httpx.AsyncClient, pw: str = "bad", **headers: str) -> int:
    page = await c.get(ROOT + "/login")
    token = re.search(r'name="login_token" value="([^"]*)"', page.text)
    assert token
    r = await c.post(
        ROOT + "/login",
        data={"username": "admin", "password": pw, "login_token": token.group(1)},
        headers=headers,
    )
    return r.status_code


def audit_actions(w: Web) -> list[str]:
    return [a.action for a in w.ctx.db.call(repo.list_audit, limit=1000)]


# ------------------------------------------------------------------------------------ W1


def test_ip_key_normalisation() -> None:
    assert ip_key("203.0.113.9") == "203.0.113.9"
    assert ip_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert ip_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert ip_key("2001:db8:1:2::1") == ip_key("2001:db8:1:2:ffff::9")
    assert ip_key("2001:db8:1:3::1") != ip_key("2001:db8:1:2::1")
    assert ip_key("garbage") is None and ip_key("") is None


async def test_ipv6_clients_in_one_slash64_share_the_budget(w: Web) -> None:
    for i in range(5):
        async with client_from(w, f"2001:db8:7:7::{i + 1}") as c:
            assert await attempt(c) == 401
    async with client_from(w, "2001:db8:7:7:ffff::1") as c:  # same /64, new address
        assert await attempt(c) == 429
    async with client_from(w, "2001:db8:7:8::1") as c:  # another /64
        assert await attempt(c) == 401


async def test_v4_mapped_and_plain_v4_are_the_same_client(w: Web) -> None:
    for _ in range(5):
        async with client_from(w, "::ffff:198.51.100.5") as c:
            assert await attempt(c) == 401
    async with client_from(w, "198.51.100.5") as c:
        assert await attempt(c) == 429


async def test_xff_robust_to_garbage_and_untrusted_peers(w: Web) -> None:
    # untrusted peer: the header is ignored, so rotating it does not help
    async with client_from(w, "203.0.113.50") as c:
        codes = [await attempt(c, **{"X-Forwarded-For": f"198.51.100.{i}"}) for i in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    # trusted proxy: the last entry counts; trailing comma / garbage never crash
    async with client_from(w, "127.0.0.1") as c:
        assert await attempt(c, **{"X-Forwarded-For": "198.51.100.1,"}) == 401
        assert await attempt(c, **{"X-Forwarded-For": ", ,"}) == 401
        assert await attempt(c, **{"X-Forwarded-For": "not-an-ip"}) == 401
        assert await attempt(c, **{"X-Forwarded-For": "1.1.1.1, ::ffff:9.9.9.9"}) == 401


async def test_global_failures_delay_but_never_lock_out(w: Web) -> None:
    async with client_from(w, "127.0.0.1") as c:
        for i in range(40):
            assert await attempt(c, **{"X-Forwarded-For": f"198.51.{i}.1"}) == 401
        # the administrator from a new address still gets in, only after an artificial delay
        assert await attempt(c, PASSWORD, **{"X-Forwarded-For": "203.0.113.200"}) == 303
    assert w.delays.values and 0 < max(w.delays.values) <= 2.0
    assert w.delays.values == sorted(w.delays.values)  # grows with the number of failures
    assert "web.login_flood" in audit_actions(w)


async def test_valid_session_is_never_throttled(aw: Web) -> None:
    for _ in range(40):
        aw.web.global_limiter.record_failure()
    r = await aw.client.get(aw.u("/users"))
    assert r.status_code == 200
    again = await aw.client.post(
        aw.u("/login"), data={"username": "x", "password": "y", "login_token": "z"}
    )
    assert again.status_code == 303 and again.headers["location"] == ROOT + "/"
    assert aw.delays.values == []


async def test_failed_logins_are_audited_in_aggregate(w: Web) -> None:
    frozen = w.clock.now
    w.web.clock = lambda: frozen
    async with client_from(w, "203.0.113.77") as c:
        for _ in range(25):
            await attempt(c)
    actions = audit_actions(w)
    assert actions.count("web.login_failed") == 1
    assert actions.count("web.login_blocked") == 1


# ------------------------------------------------------------------------------------ W2


async def test_argon2_runs_in_a_thread(w: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    calls: list[str] = []
    real = asyncio.to_thread

    async def spy(fn: Any, /, *args: Any, **kw: Any) -> Any:
        calls.append(getattr(fn, "__name__", "?"))
        return await real(fn, *args, **kw)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    assert (await w.login()).status_code == 303
    assert "verify" in calls
    new = "another long passphrase 42"
    await w.post("/settings/password", {"current": PASSWORD, "new": new, "again": new})
    assert "hash" in calls and calls.count("verify") >= 2
    assert "asyncio.to_thread" in (WEB_DIR.parent / "services" / "admin.py").read_text()


# ------------------------------------------------------------------------------------ W3


def test_parse_helpers_are_ascii_only() -> None:
    assert parse_uint("123") == 123 and parse_uint(" 7 ") == 7
    for bad in ("²", "١٢", "-1", "+1", "1.5", "", "1" * 19, "１２"):
        assert parse_uint(bad) is None, bad
    assert (
        parse_decimal("1,5") == 1.5 and parse_decimal("٣") is None and parse_decimal("1e9") is None
    )
    assert parse_day("2026-01-02") and parse_day("9999-12-31") is None
    assert parse_day("２０２６-01-02") is None
    assert valid_username("alice_x") and valid_username("") and not valid_username("ab")
    assert not valid_username("al ice9") and not valid_username("1alice")


BAD_NUMBERS = ["²", "١٢٣", "9" * 40]


@pytest.mark.parametrize("bad", BAD_NUMBERS)
async def test_numbers_on_every_route_never_500(aw: Web, bad: str) -> None:
    (uid,) = await aw.create_users("alice")
    pre = aw.ctx.db.call(repo.all_users)
    get_urls = [
        f"/users?page={bad}",
        f"/users?per={bad}",
        f"/users?expires_days={bad}",
        f"/users?traffic_min={bad}",
        f"/users?traffic_max={bad}",
        f"/audit?page={bad}",
        f"/users/{bad}",
        f"/users/{bad}/traffic.json",
        f"/broadcast/{bad}",
    ]
    for url in get_urls:
        r = await aw.client.get(aw.u(url))
        assert r.status_code in (400, 404), (url, r.status_code)
    posts: list[tuple[str, dict[str, Any]]] = [
        ("/users/bulk", {"action": "extend", "days": bad, "ids": [str(uid)]}),
        ("/users/bulk", {"action": "disable", "all_matching": "1", "expected_total": bad}),
        ("/users/bulk", {"action": "disable", "ids": [bad]}),
        (f"/users/{uid}/action", {"action": "extend", "days": bad}),
        (f"/users/{uid}/meta", {"name": "alice", "tg_id": bad}),
        ("/users/new", {"mode": "single", "name": "bob", "tg_id": bad, "term": "default"}),
        ("/users/new", {"mode": "count", "prefix": "p", "count": bad, "term": "default"}),
        ("/users/new", {"mode": "list", "list": f"bob;{bad};x", "term": "default"}),
        ("/settings/admins", {"action": "add", "tg_id": bad}),
        ("/settings", {"secrets_per_process": bad}),
        (f"/users/{bad}/action", {"action": "disable"}),
        (f"/users/{bad}/reveal", {}),
        (f"/backups/{bad}/download", {"password": PASSWORD}),
        (f"/requests/{bad}/approve", {"term": "default"}),
        (f"/backups/{bad}/restore", {"confirm": "восстановить"}),
        ("/import/confirm", {"n": bad, "ack_old_bot": "1"}),
    ]
    for url, data in posts:
        r = await aw.post(url, data)
        assert r.status_code in (303, 400, 404, 422), (url, r.status_code)
    after = aw.ctx.db.call(repo.all_users)
    assert [u.name for u in after] == [u.name for u in pre]
    assert after[0].tg_id is None and after[0].expires_at == pre[0].expires_at
    assert aw.ctx.db.call(repo.list_admins) == []


# ------------------------------------------------------------------------------------ W4


@pytest.mark.parametrize("path", ["/users/0", "/users/99999999999999999999", "/users/-1"])
async def test_out_of_range_path_ids_are_rejected(aw: Web, path: str) -> None:
    assert (await aw.client.get(aw.u(path))).status_code in (400, 404)


async def test_date_overflow_and_qs_field_limits(aw: Web) -> None:
    await aw.create_users("alice")
    for q in ("created_to=9999-12-31", "created_from=0001-01-01", "seen_to=9999-12-31"):
        assert (await aw.client.get(aw.u("/users?" + q))).status_code == 400
    many = "&".join(f"q{i}=x" for i in range(100))
    r = await aw.post(
        "/users/bulk",
        {"action": "disable", "all_matching": "1", "expected_total": "1", "filter_qs": many},
    )
    assert r.status_code == 400


async def test_non_ascii_tokens_do_not_crash(aw: Web, w: Web) -> None:
    (uid,) = await aw.create_users("alice")
    r = await aw.client.post(
        aw.u(f"/users/{uid}/action"), data={"action": "disable", "csrf_token": "токен"}
    )
    assert r.status_code == 403
    r = await aw.client.post(
        aw.u(f"/users/{uid}/action"),
        data={"action": "disable"},
        headers=[(b"X-CSRF-Token", b"tok\xe9n")],
    )
    assert r.status_code == 403
    aw.client.cookies.clear()
    page = await aw.client.get(aw.u("/login"))
    assert page.status_code == 200
    r = await aw.client.post(
        aw.u("/login"), data={"username": "a", "password": "b", "login_token": "тест"}
    )
    assert r.status_code == 400


async def test_500_logs_type_and_scrubbed_traceback(
    aw: Web, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ab" * 16

    async def boom() -> dict[str, Any]:
        raise RuntimeError(f"failed with {secret}")

    monkeypatch.setattr(aw.ctx.settings, "all", boom)
    caplog.set_level(logging.DEBUG)
    r = await aw.client.get(aw.u("/settings"))
    assert r.status_code == 500 and secret not in r.text
    assert "RuntimeError" in caplog.text and "Traceback" in caplog.text
    assert secret not in caplog.text and "[redacted]" in caplog.text
    assert "content-security-policy" in r.headers


# ------------------------------------------------------------------------------------ W5


async def test_body_over_2mb_is_413(aw: Web) -> None:
    big = "x" * (2 * 1024 * 1024 + 10)
    r = await aw.client.post(aw.u("/import/preview"), data={"csv_text": big})
    assert r.status_code == 413

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(6):
            yield b"a=" + b"x" * (512 * 1024) + b"&"

    r = await aw.client.post(
        aw.u("/import/preview"),
        content=chunks(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": await aw.csrf(),
        },
    )
    assert r.status_code == 413, r.text


async def test_host_header_allow_list(w: Web) -> None:
    r = await w.client.get(w.u("/login"), headers={"Host": "evil.example.org"})
    assert r.status_code == 400
    assert (
        await w.client.get(w.u("/login"), headers={"Host": "127.0.0.1:8090"})
    ).status_code == 200
    assert (await w.client.get(w.u("/login"), headers={"Host": "localhost"})).status_code == 200
    w.ctx.db.call(repo.set_setting, "panel_hostname", "panel.example.com")
    async with client_from(w, "203.0.113.3") as c:  # fresh app: fresh allow-list cache
        ok = await c.get(ROOT + "/login", headers={"Host": "panel.example.com"})
        assert ok.status_code == 200
        bad = await c.get(ROOT + "/login", headers={"Host": "other.example.com"})
        assert bad.status_code == 400


def test_running_doc_lists_server_flags() -> None:
    text = (WEB_DIR / "RUNNING.md").read_text()
    for needle in (
        "127.0.0.1",
        "access_log=False",
        "proxy_headers=False",
        "server_header=False",
        "limit_concurrency",
        "timeout_keep_alive=5",
        "h11_max_incomplete_event_size",
        "TGPANEL_SECRET_KEY",
        "TGPANEL_ENV_FILE",
    ):
        assert needle in text, needle


# ------------------------------------------------------------------------------------ W11


def test_routes_and_adapters_do_not_touch_system_ops() -> None:
    files = [*(WEB_DIR / "routes").glob("*.py"), WEB_DIR / "adapters.py"]
    for path in files:
        text = path.read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(("#", '"""')))
        for forbidden in ("pipeline.ops", ".ops.", "import SystemOps", "ops: SystemOps"):
            assert forbidden not in code, (path.name, forbidden)
        if path.name != "auth.py":
            assert "db_write(" not in code, path.name


async def test_backup_service_audits_streams_and_caps_download(aw: Web) -> None:
    await aw.create_users("alice")
    await aw.web.backups.create("web:admin")
    rec = next(b for b in await aw.web.backups.list() if b.reason == "manual")
    dl = await aw.web.backups.open_download(rec.id, "web:admin")
    assert dl is not None and dl.size == len(aw.fake.files[rec.path].data)
    data = b"".join([c async for c in dl.chunks])
    assert data == aw.fake.files[rec.path].data
    assert "backup.download" in audit_actions(aw)
    assert await aw.web.backups.open_download(9999, "web:admin") is None
    opener = aw.web.backups._opener
    small = BackupService(aw.ctx.pipeline, max_download=10, opener=opener)
    with pytest.raises(BackupError):
        await small.open_download(rec.id, "web:admin")
    # the slot is free again after the failed attempt
    assert await small.open_download(9999, "web:admin") is None


async def test_download_reads_in_blocks_not_whole_file(aw: Web) -> None:
    await aw.create_users("alice")
    await aw.web.backups.create("web:admin")
    rec = next(b for b in await aw.web.backups.list() if b.reason == "manual")

    class Spy(io.BytesIO):
        sizes: ClassVar[list[int]] = []

        def read(self, n: int | None = -1) -> bytes:
            Spy.sizes.append(-1 if n is None else n)
            return super().read(n)

    big = Spy(aw.fake.files[rec.path].data * 50)
    svc = BackupService(aw.ctx.pipeline, opener=lambda path: big)
    dl = await svc.open_download(rec.id, "web:admin")
    assert dl is not None
    blocks = [c async for c in dl.chunks]
    assert (
        len(blocks) >= 1
        and all(n > 0 for n in Spy.sizes)
        and max(len(b) for b in blocks) <= 1 << 20
    )


async def test_only_one_concurrent_download(aw: Web) -> None:
    await aw.create_users("alice")
    await aw.web.backups.create("web:admin")
    rec = next(b for b in await aw.web.backups.list() if b.reason == "manual")
    first = await aw.web.backups.open_download(rec.id, "web:admin")
    assert first is not None
    with pytest.raises(DownloadBusy):
        await aw.web.backups.open_download(rec.id, "web:admin")
    r = await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})
    assert r.status_code == 409
    first.release()
    ok = await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})
    assert ok.status_code == 200
    # finished stream frees the slot again
    assert (await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})).status_code == 200


async def test_stale_download_slot_expires(aw: Web) -> None:
    now = [0.0]
    svc = BackupService(aw.ctx.pipeline, opener=aw.web.backups._opener, monotonic=lambda: now[0])
    await aw.create_users("alice")
    await aw.web.backups.create("web:admin")
    rec = next(b for b in await aw.web.backups.list() if b.reason == "manual")
    assert await svc.open_download(rec.id, "a") is not None
    with pytest.raises(DownloadBusy):
        await svc.open_download(rec.id, "a")
    now[0] = 1801.0
    assert await svc.open_download(rec.id, "a") is not None


async def test_download_and_restore_need_the_panel_password(aw: Web) -> None:
    await aw.create_users("alice")
    await aw.post("/backups/create")
    rec = next(b for b in aw.ctx.db.call(repo.list_backups) if b.reason == "manual")
    for data in ({}, {"password": "wrong"}):
        r = await aw.post(f"/backups/{rec.id}/download", data)
        assert r.status_code == 303  # no archive, back to the list with an error
        assert b"gzip" not in r.content
    assert "backup.download" not in audit_actions(aw)
    r = await aw.post(
        f"/backups/{rec.id}/restore", {"confirm": "восстановить", "password": "wrong"}
    )
    page = await aw.client.get(r.headers["location"])
    assert "Текущий пароль неверен" in page.text or "Пароль" in page.text
    assert "backup.restore" not in audit_actions(aw)
    assert (await aw.client.get(aw.u(f"/backups/{rec.id}/download"))).status_code == 405
    ok = await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})
    assert ok.status_code == 200 and ok.content == aw.fake.files[rec.path].data


async def test_reauth_failures_are_rate_limited(aw: Web) -> None:
    await aw.create_users("alice")
    await aw.post("/backups/create")
    rec = next(b for b in aw.ctx.db.call(repo.list_backups) if b.reason == "manual")
    for _ in range(5):
        await aw.post(f"/backups/{rec.id}/download", {"password": "wrong"})
    r = await aw.post(f"/backups/{rec.id}/download", {"password": PASSWORD})
    page = await aw.client.get(r.headers["location"])
    assert r.status_code == 303 and "Слишком много попыток" in page.text
    assert "backup.download" not in audit_actions(aw)


async def test_dashboard_service_caches_cert_lookup_and_fills_limit(aw: Web) -> None:
    aw.ctx.db.call(repo.set_setting, "panel_hostname", "panel.example.com")
    aw.ctx.db.call(repo.set_setting, "max_sessions_global", "2048")
    ops = aw.ctx.pipeline.ops
    calls: list[str] = []
    real = ops.tls_cert_info

    async def spy(hostname: str) -> Any:
        calls.append(hostname)
        return await real(hostname)

    ops.tls_cert_info = spy  # type: ignore[method-assign]
    now = [0.0]
    svc = DashboardService(
        TrafficService(aw.ctx.db, aw.clock), ops, aw.ctx.db, monotonic=lambda: now[0]
    )
    view = await svc.view()
    await svc.view()
    assert calls == ["panel.example.com"] and view.max_sessions_global == 2048
    now[0] = 3601.0
    await svc.view()
    assert len(calls) == 2


# ------------------------------------------------------------------------------------ W12


async def test_download_is_audited_and_streamed(aw: Web) -> None:
    aw.web.backups = aw.web.backups
    await aw.create_users("alice")
    await aw.post("/backups/create")
    rec = next(b for b in aw.ctx.db.call(repo.list_backups) if b.reason == "manual")
    form = {"password": PASSWORD, "csrf_token": await aw.csrf()}
    async with aw.client.stream("POST", aw.u(f"/backups/{rec.id}/download"), data=form) as r:
        assert r.status_code == 200 and r.headers["content-length"]
        body = b"".join([c async for c in r.aiter_bytes()])
    assert body == aw.fake.files[rec.path].data
    entry = next(
        a for a in aw.ctx.db.call(repo.list_audit, limit=50) if a.action == "backup.download"
    )
    assert entry.actor == "web:admin" and entry.target == f"backup:{rec.id}"


async def test_all_matching_requires_expected_total(aw: Web) -> None:
    await aw.create_users("alice")
    r = await aw.post("/users/bulk", {"action": "disable", "all_matching": "1", "filter_qs": ""})
    assert r.status_code == 303
    assert {u.status.value for u in aw.ctx.db.call(repo.all_users)} == {"active"}


async def test_bulk_comment_is_one_batched_write(aw: Web) -> None:
    ids = await aw.create_users("a", "b", "c", "d")
    calls = {"n": 0}
    real = aw.ctx.pipeline.db_write

    async def counting(fn: Any, /, *args: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return await real(fn, *args, **kw)

    aw.ctx.pipeline.db_write = counting  # type: ignore[method-assign]
    await aw.post(
        "/users/bulk", {"action": "set_comment", "comment": "батч", "ids": [str(i) for i in ids]}
    )
    assert calls["n"] == 1
    assert {u.comment for u in aw.ctx.db.call(repo.all_users)} == {"батч"}


async def test_inline_comment_inputs_are_outside_any_form(aw: Web) -> None:
    await aw.create_users("alice", "bob")
    html = (await aw.client.get(aw.u("/users"))).text
    assert html.index("</form>", html.index('id="bulk-form"')) < html.index("<table")
    assert html.count('name="inline_comment"') == 2
    table = html[html.index("<table") :]
    assert "<form" not in table.split("</table>")[0]
    assert 'name="ids" value="1" form="bulk-form"' in html


async def test_set_expiry_in_the_past_needs_confirmation(aw: Web) -> None:
    (uid,) = await aw.create_users("alice")
    before = (await aw.ctx.users.get(uid)).expires_at  # type: ignore[union-attr]
    r = await aw.post(
        f"/users/{uid}/action", {"action": "set_expiry", "expires_date": "2001-01-01"}
    )
    page = await aw.client.get(r.headers["location"])
    assert "в прошлом" in page.text
    assert (await aw.ctx.users.get(uid)).expires_at == before  # type: ignore[union-attr]
    await aw.post(
        f"/users/{uid}/action",
        {"action": "set_expiry", "expires_date": "2001-01-01", "confirm_past": "1"},
    )
    assert (await aw.ctx.users.get(uid)).expires_at.year == 2001  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", ["ab", "x" * 33, "1abcde", "al ice", "алиса_x", "a-b-c-d"])
async def test_tg_username_validation(aw: Web, bad: str) -> None:
    (uid,) = await aw.create_users("alice")
    r = await aw.post(f"/users/{uid}/meta", {"name": "alice", "tg_username": bad})
    page = await aw.client.get(r.headers["location"])
    assert "Username:" in page.text
    extra = aw.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.tg_username is None


async def test_clear_tg_id_from_card(aw: Web) -> None:
    (uid,) = await aw.create_users("alice", tg_id=None)
    await aw.post(f"/users/{uid}/meta", {"name": "alice", "tg_id": "4242"})
    assert (await aw.ctx.users.get(uid)).tg_id == 4242  # type: ignore[union-attr]
    await aw.post(f"/users/{uid}/meta", {"name": "alice", "tg_id": "4242", "clear_tg_id": "1"})
    assert (await aw.ctx.users.get(uid)).tg_id is None  # type: ignore[union-attr]


def test_signer_uses_sha256_and_cookie_path_has_trailing_slash(w: Web) -> None:
    ser = Signer(SECRET_KEY)._ser("session")
    assert ser.signer_kwargs["digest_method"] is hashlib.sha256


async def test_cookie_path_trailing_slash_and_flows_still_work(w: Web) -> None:
    r = await w.login()
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("tgp_session="))
    assert f"Path={ROOT}/;" in cookie
    assert (await w.client.get(w.u("/users"))).status_code == 200
    out = await w.post("/settings/logout-all")
    assert out.status_code == 303 and out.headers["location"] == ROOT + "/login"
    assert (await w.client.get(w.u("/users"))).status_code == 303
    # flash cookie (set by a redirect) is delivered back under the same path
    assert (await w.login()).status_code == 303
    r2 = await w.post("/users/bulk", {"action": "disable"})
    assert "Path=" + ROOT + "/" in r2.headers["set-cookie"]
    page = await w.client.get(r2.headers["location"])
    assert "Ничего не выбрано" in page.text


def test_vendored_hashes_match_versions_file() -> None:
    text = (WEB_DIR / "static" / "vendor" / "VERSIONS.txt").read_text()
    for name in ("htmx.min.js", "echarts.min.js"):
        digest = hashlib.sha256((WEB_DIR / "static" / "vendor" / name).read_bytes()).hexdigest()
        assert digest in text, name


async def test_templates_and_token_services(aw: Web) -> None:
    await aw.web.admin.set_templates({"msg.link": "x {link}"}, "web:admin")
    assert (await aw.web.admin.templates())["msg.link"] == "x {link}"
    token = "123456789:" + "B" * 35
    await aw.web.admin.set_bot_token(token, "web:admin")
    assert aw.env_writes[-1] == ("TGPANEL_BOT_TOKEN", token)
    assert await aw.web.admin.bot_token_set_at()


# --------------------------------------------------------------------------- S12 and nits


async def test_bot_token_message_says_what_happened(aw: Web) -> None:
    token = "123456789:" + "C" * 35
    r = await aw.post("/settings/bot-token", {"token": token})
    page = await aw.client.get(r.headers["location"])
    assert "бот перезапущен" in page.text and aw.bot.calls == 1
    aw.bot.result = False
    r = await aw.post("/settings/bot-token", {"token": "987654321:" + "D" * 35})
    page = await aw.client.get(r.headers["location"])
    assert "перезапустите службу" in page.text and aw.bot.calls == 2
    assert aw.ctx.db.call(repo.get_setting, "bot_token") is None


async def test_logout_ends_all_sessions_only_when_ticked(aw: Web) -> None:
    assert "завершить все сессии" in (await aw.client.get(aw.u("/"))).text
    version = aw.ctx.db.call(repo.get_setting, "panel_session_version")
    r = await aw.post("/logout")
    assert r.status_code == 303
    assert aw.ctx.db.call(repo.get_setting, "panel_session_version") == version
    assert (await aw.login()).status_code == 303
    await aw.post("/logout", {"all_sessions": "1"})
    assert aw.ctx.db.call(repo.get_setting, "panel_session_version") != version


async def test_argon2_rehash_after_login(w: Web) -> None:
    from argon2 import PasswordHasher

    from tgpanel.services.admin import AdminService

    stronger = PasswordHasher(time_cost=2, memory_cost=8, parallelism=1)
    w.web.admin = AdminService(w.ctx.pipeline, hasher=stronger)
    old = w.ctx.db.call(repo.get_setting, "panel_password_hash")
    assert (await w.login()).status_code == 303
    new = w.ctx.db.call(repo.get_setting, "panel_password_hash")
    assert new != old and not stronger.check_needs_rehash(str(new))
    assert stronger.verify(str(new), PASSWORD)
    w.client.cookies.clear()
    assert (await w.login()).status_code == 303  # still works with the new hash
    assert "web.password_rehash" in audit_actions(w)


async def test_wrong_current_password_attempts_are_limited(aw: Web) -> None:
    new = "another long passphrase 42"
    for _ in range(5):
        await aw.post("/settings/password", {"current": "bad", "new": new, "again": new})
    r = await aw.post("/settings/password", {"current": PASSWORD, "new": new, "again": new})
    page = await aw.client.get(r.headers["location"])
    assert "Слишком много попыток" in page.text
    assert aw.ctx.db.call(repo.get_setting, "panel_session_version") == "1"


def test_first_run_credentials_never_referenced_by_the_web_layer() -> None:
    for path in [*WEB_DIR.rglob("*.py"), *WEB_DIR.rglob("*.html"), *WEB_DIR.rglob("*.js")]:
        text = path.read_text()
        assert "first-run" not in text and "first_run" not in text, path.name
    for path in (WEB_DIR.parent / "services").glob("*.py"):
        if path.name in ("admin.py", "backups.py", "bulk.py", "dashboard.py"):
            assert "first-run-credentials" not in path.read_text(), path.name
