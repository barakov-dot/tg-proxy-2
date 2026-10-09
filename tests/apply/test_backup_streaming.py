"""B2 (slim/full snapshots, streaming archives) and the supporting system-layer fixes."""

from __future__ import annotations

import asyncio
import io
import os
import socket
import sqlite3
import ssl
import tarfile
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.apply.conftest import Env, add_users
from tgpanel.apply import backup as bk
from tgpanel.apply.config import ApplyPaths
from tgpanel.apply.runtime import ensure_runtime_dirs
from tgpanel.db import repo
from tgpanel.domain.import_ import SourceProfile, check_id_regex_safety, plan_import
from tgpanel.system import real as real_mod
from tgpanel.system import validation as val
from tgpanel.system.ops import LocalFile, SystemOpsError
from tgpanel.system.real import RealSystemOps
from tgpanel.system.validation import scrub

PROFILES = "/etc/tproxy-server/profiles.json"


def add_traffic_rows(env: Env, user_ids: list[int]) -> None:
    for uid in user_ids:
        env.db.call(repo.add_traffic, "day", uid, env.clock(), bytes_up=100 + uid, bytes_down=50)
        env.db.call(repo.add_traffic, "minute", uid, env.clock(), bytes_up=1, bytes_down=2)


def traffic_count(env: Env, uid: int) -> int:
    return int(
        env.db.call(
            lambda c: c.execute(
                "SELECT (SELECT COUNT(*) FROM traffic_day WHERE user_id = ?)"
                " + (SELECT COUNT(*) FROM traffic_minute WHERE user_id = ?)",
                (uid, uid),
            ).fetchone()[0]
        )
    )


def snapshot_tables(env: Env, backup_path: str) -> sqlite3.Connection:
    members = val.parse_tar_gz(env.fake.files[backup_path].data)
    mem = sqlite3.connect(":memory:")
    mem.deserialize(members[bk.DB_MEMBER])
    return mem


def manifest(env: Env, backup_path: str) -> dict[str, Any]:
    import json

    return json.loads(val.parse_tar_gz(env.fake.files[backup_path].data)[bk.MANIFEST_NAME])  # type: ignore[no-any-return]


# ===================================================================================== B2


async def test_pre_apply_backup_is_slim_and_manual_backup_is_full(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    add_traffic_rows(env, [1, 2])
    env.db.call(repo.add_audit, env.clock(), "web:x", "something", "", "")
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="second")
    run = env.db.call(repo.get_apply_run, out.apply_run_id)
    assert run and run.backup_path
    slim = snapshot_tables(env, run.backup_path)
    try:
        assert manifest(env, run.backup_path)["slim"] is True
        assert slim.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        for table in (
            "traffic_minute",
            "traffic_day",
            "traffic_hour",
            "counter_state",
            "audit_log",
        ):
            assert slim.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        assert slim.execute("SELECT value FROM settings WHERE key = 'proxy_hostname'").fetchone()
    finally:
        slim.close()
    info = await env.pipeline.create_backup("daily", "system")  # FULL by default
    full = snapshot_tables(env, info.path)
    try:
        assert manifest(env, info.path)["slim"] is False
        assert full.execute("SELECT COUNT(*) FROM traffic_day").fetchone()[0] == 2
        assert full.execute("SELECT COUNT(*) FROM traffic_minute").fetchone()[0] == 2
        assert full.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 1
    finally:
        full.close()
    thin = await env.pipeline.create_backup("thin", "system", full=False)
    assert manifest(env, thin.path)["slim"] is True


async def test_slim_snapshot_works_for_in_memory_databases_too(tmp_path: Path) -> None:
    from tgpanel.db.connection import Database

    db = Database(":memory:")
    db.call(repo.set_setting, "k", "v")
    dest = tmp_path / "s.db"
    bk.snapshot_database(db, None, dest, slim=True)
    out = sqlite3.connect(dest)
    try:
        assert out.execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0] == "v"
    finally:
        out.close()
    assert oct(dest.stat().st_mode & 0o777) == "0o600"


async def test_restore_of_a_slim_backup_keeps_current_statistics(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")  # user 3 is new
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path  # slim snapshot of the state with users 1 and 2
    add_traffic_rows(env, [1, 2, 3])
    before = {uid: traffic_count(env, uid) for uid in (1, 2, 3)}
    assert before == {1: 2, 2: 2, 3: 2}
    relay = env.fake.restart_count("tproxy-server")
    pool = env.fake.restart_count("tgpanel-mtproxy@1")
    res = await env.pipeline.restore_backup(run.backup_path, "system")
    assert res.ok, res.error
    assert [u.id for u in env.users()] == [1, 2]
    assert traffic_count(env, 1) == 2 and traffic_count(env, 2) == 2  # statistics survive
    assert traffic_count(env, 3) == 0  # the vanished user's statistics go with the user
    assert [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]] == ["u1", "u2"]
    assert env.fake.restart_count("tproxy-server") == relay + 1
    assert env.fake.restart_count("tgpanel-mtproxy@1") == pool + 1


async def test_restore_of_a_full_backup_replaces_statistics(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    add_traffic_rows(env, [1])
    info = await env.pipeline.create_backup("full", "system")
    add_traffic_rows(env, [1, 2])  # more traffic after the backup
    assert traffic_count(env, 2) == 2
    res = await env.pipeline.restore_backup(info.path, "system")
    assert res.ok, res.error
    assert traffic_count(env, 1) == 2 and traffic_count(env, 2) == 0


async def test_failed_slim_restore_keeps_everything(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    add_traffic_rows(env, [1, 2, 3])
    env.fake.fail_on("systemctl", "restart tproxy-server")
    res = await env.pipeline.restore_backup(run.backup_path, "system")
    assert not res.ok
    assert [u.id for u in env.users()] == [1, 2, 3]
    assert all(traffic_count(env, uid) == 2 for uid in (1, 2, 3))


async def test_backup_dir_holds_no_temp_snapshots_afterwards(env: Env, tmp_path: Path) -> None:
    import tempfile

    before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("tgpanel-")}
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    await env.pipeline.create_backup("m", "system")
    env.fake.fail_on("systemctl", "restart tproxy-server")
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="y")
    after = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("tgpanel-")}
    assert after == before


# ---------------------------------------------------------------- streaming archives


def test_default_cap_is_a_high_safety_limit() -> None:
    assert val.MAX_TAR_TOTAL_BYTES == 4 * 1024 * 1024 * 1024


def test_big_database_is_streamed_into_the_archive_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A '>256 MiB' payload is simulated by lowering the cap: 3 MB against a 2 MB cap."""
    big = tmp_path / "big.db"
    big.write_bytes(os.urandom(3 * 1024 * 1024))
    monkeypatch.setattr(val, "MAX_TAR_TOTAL_BYTES", 2 * 1024 * 1024)
    dest = tmp_path / "out.tar.gz"
    with pytest.raises(SystemOpsError, match="too large"):
        real_mod.write_tar_stream_sync(str(dest), {"tgpanel.db": LocalFile(str(big))})
    assert not dest.exists() and not [
        p for p in tmp_path.iterdir() if p.name.startswith(".tgpanel-")
    ]
    # under the cap it works and the content round-trips through the streaming extractor
    monkeypatch.setattr(val, "MAX_TAR_TOTAL_BYTES", 8 * 1024 * 1024)
    real_mod.write_tar_stream_sync(str(dest), {"tgpanel.db": LocalFile(str(big)), "m": b"{}"})
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    out = tmp_path / "restored.db"
    assert val.extract_tar_member_to_file(str(dest), "tgpanel.db", str(out)) is True
    assert out.read_bytes() == big.read_bytes() and oct(out.stat().st_mode & 0o777) == "0o600"
    assert val.extract_tar_member_to_file(str(dest), "absent", str(tmp_path / "n")) is False
    # reading with a lower cap fails while streaming (declared sizes are summed)
    monkeypatch.setattr(val, "MAX_TAR_TOTAL_BYTES", 1024 * 1024)
    with pytest.raises(SystemOpsError, match="too large"):
        val.iter_tar_members(str(dest), {"m"})
    with pytest.raises(SystemOpsError, match="too large"):
        val.parse_tar_gz(str(dest))


def test_archive_writer_never_reads_a_local_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "db.bin"
    src.write_bytes(b"x" * 100_000)
    sizes: list[int] = []
    real_open = open

    class Spy(io.RawIOBase):
        def __init__(self) -> None:
            self.f = real_open(src, "rb")

        def readable(self) -> bool:
            return True

        def readinto(self, b: Any) -> int:
            sizes.append(len(b))
            return self.f.readinto(b)

    monkeypatch.setattr(val, "open", lambda *a, **k: io.BufferedReader(Spy()), raising=False)
    buf = io.BytesIO()
    val.write_tar_gz(buf, {"d": LocalFile(str(src))})
    assert sizes and max(sizes) < 100_000  # chunked reads only
    assert val.parse_tar_gz(buf.getvalue())["d"] == b"x" * 100_000


def test_reader_validates_every_header_while_streaming() -> None:
    buf = io.BytesIO()
    import gzip

    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        ok = tarfile.TarInfo("ok")
        ok.size = 1
        tar.addfile(ok, io.BytesIO(b"1"))
        bad = tarfile.TarInfo("../evil")
        bad.size = 1
        tar.addfile(bad, io.BytesIO(b"x"))
    with pytest.raises(SystemOpsError):
        val.iter_tar_members(buf.getvalue(), {"ok"})


async def test_fake_archive_ops_match_the_contract(env: Env) -> None:
    info = await env.pipeline.create_backup("m", "system")
    members = await env.fake.read_tar_members(info.path, {bk.MANIFEST_NAME})
    assert set(members) == {bk.MANIFEST_NAME}
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "x.db"
        assert await env.fake.extract_tar_member(info.path, bk.DB_MEMBER, str(target)) is True
        assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM settings").fetchone()[0] > 0


# ===================================================================================== S7/S8


def test_s7_lock_lives_in_the_state_directory() -> None:
    paths = ApplyPaths()
    assert paths.lock == "/var/lib/tgpanel/apply.lock" and paths.state_dir == "/var/lib/tgpanel"


async def test_s7_runtime_dirs_are_created_0700(env: Env) -> None:
    env.fake.clear_calls()
    await ensure_runtime_dirs(env.fake, env.config.paths)
    calls = [c[1:] for c in env.fake.calls_of("ensure_dir")]
    assert calls == [
        ("/etc/tgpanel", 0o700, "root", "root"),
        ("/etc/tgpanel/mtproxy", 0o700, "root", "root"),
        ("/var/backups/tgpanel", 0o700, "root", "root"),
        ("/var/lib/tgpanel", 0o700, "root", "root"),
    ]


async def test_s7_container_start_runs_recovery_and_loads_the_hostname(
    tmp_path: Path,
) -> None:
    from tests.apply.conftest import Clock, no_sleep
    from tgpanel.apply.config import ApplyConfig
    from tgpanel.services.container import build_context
    from tgpanel.system.fake import FakeSystemOps

    fake = FakeSystemOps()
    fake.seed_upstream("clean")
    ctx = build_context(
        fake, tmp_path / "c.db", config=ApplyConfig(), clock=Clock(), sleep=no_sleep
    )
    try:
        ctx.db.call(repo.set_setting, "proxy_hostname", "start.example.com")
        report = await ctx.start()
        assert report is not None and report.run_ids == []
        assert len(fake.calls_of("ensure_dir")) == 4 and fake.calls_of("acquire_lock")
        user = type("U", (), {"secret": "a" * 32})()
        assert ctx.users.link(user).startswith("https://t.me/webproxy?server=start.example.com")
        assert await ctx.start(recover=False) is None
    finally:
        ctx.close()


async def test_s7_real_lock_does_not_leak_descriptors_on_cancel_or_timeout(tmp_path: Path) -> None:
    ops = RealSystemOps()
    lock = str(tmp_path / "x.lock")

    def fds() -> int:
        return len(os.listdir("/dev/fd"))

    holder = await ops.acquire_lock(lock, 1.0)
    base = fds()
    waiter = asyncio.create_task(ops.acquire_lock(lock, 5.0))
    await asyncio.sleep(0.2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert fds() == base
    with pytest.raises(SystemOpsError, match="timed out"):
        await ops.acquire_lock(lock, 0.1)
    assert fds() == base
    await holder.release()
    again = await ops.acquire_lock(lock, 1.0)
    await again.release()
    assert fds() == base - 1


async def test_s7_real_ensure_dir(tmp_path: Path) -> None:
    ops = RealSystemOps()
    target = tmp_path / "a" / "b"
    me = os.getlogin() if hasattr(os, "getlogin") else "root"
    import grp
    import pwd

    uid = os.geteuid()
    owner = pwd.getpwuid(uid).pw_name
    group = grp.getgrgid(os.getegid()).gr_name
    _ = me
    await ops.ensure_dir(str(target), 0o700, owner, group)
    assert target.is_dir() and oct(target.stat().st_mode & 0o777) == "0o700"


class _FakeTlsSock:
    def __init__(self, cert: dict[str, Any] | None, raises: Exception | None) -> None:
        self.cert, self.raises = cert, raises

    def __enter__(self) -> _FakeTlsSock:
        if self.raises:
            raise self.raises
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def getpeercert(self) -> dict[str, Any] | None:
        return self.cert


class _FakeCtx:
    def __init__(self, cert: dict[str, Any] | None, raises: Exception | None) -> None:
        self.cert, self.raises = cert, raises
        self.check_hostname = True
        self.verify_mode = ssl.CERT_NONE

    def wrap_socket(self, sock: object, server_hostname: str) -> _FakeTlsSock:
        return _FakeTlsSock(self.cert, self.raises)


CERT = {
    "issuer": ((("organizationName", "Example CA"),),),
    "notBefore": "Jan  1 00:00:00 2026 GMT",
    "notAfter": "Apr  1 00:00:00 2026 GMT",
    "subjectAltName": (("DNS", "panel.example.com"), ("DNS", "*.wild.example.com")),
}


def _patch_tls(
    monkeypatch: pytest.MonkeyPatch, cert: dict[str, Any] | None, raises: Exception | None = None
) -> _FakeCtx:
    ctx = _FakeCtx(cert, raises)
    monkeypatch.setattr(ssl, "create_default_context", lambda: ctx)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeTlsSock(None, None))
    return ctx


def test_s8_cert_info_uses_only_the_public_api(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _patch_tls(monkeypatch, CERT)
    info = real_mod._tls_cert_info_sync("panel.example.com", 443, 1.0)
    assert info is not None and info.valid_chain and info.issuer == "Example CA"
    assert info.not_after == "2026-04-01T00:00:00Z"
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is False
    wild = real_mod._tls_cert_info_sync("a.wild.example.com", 443, 1.0)
    assert wild is not None and wild.valid_chain
    other = real_mod._tls_cert_info_sync("evil.example.org", 443, 1.0)
    assert other is not None and other.valid_chain is False and other.issuer == "Example CA"
    source = Path(real_mod.__file__).read_text()
    assert "_test_decode_cert" not in source and "_ssl" not in source.replace("_ssl_", "")


def test_s8_chain_failure_returns_an_invalid_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tls(monkeypatch, None, ssl.SSLCertVerificationError("bad chain"))
    info = real_mod._tls_cert_info_sync("panel.example.com", 443, 1.0)
    assert info is not None
    assert (info.issuer, info.not_before, info.not_after, info.valid_chain) == ("", "", "", False)
    _patch_tls(monkeypatch, None, OSError("refused"))
    assert real_mod._tls_cert_info_sync("panel.example.com", 443, 1.0) is None


# ===================================================================================== S9


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(a+)+$",
        r"(\d*)*x",
        r"((a|b+){2,})",
        r"(?=x)(\d+)",
        r"(?!x)(\d+)",
        r"(?<=a)(\d+)",
        r"(a)\1",
        r"(?P<n>a)(?P=n)",
        r"(\d+)(?(1)a|b)",
    ],
)
def test_s9_dangerous_patterns_are_rejected(pattern: str) -> None:
    assert check_id_regex_safety(pattern) is not None
    plan = plan_import(
        [SourceProfile("user_12345", "a" * 32, None, "127.0.0.1:2398")], id_regex=pattern
    )
    assert plan.errors and plan.rows[0].tg_id is None


@pytest.mark.parametrize(
    "pattern", [r"^user_(\d{5,15})$", r"^u(?:ser)?_(\d+)$", r"^id-([0-9]{3,})$"]
)
def test_s9_ordinary_patterns_still_work(pattern: str) -> None:
    assert check_id_regex_safety(pattern) is None


def test_s9_catastrophic_pattern_never_runs_and_long_names_are_bounded() -> None:
    started = time.monotonic()
    evil = SourceProfile("a" * 40 + "!", "a" * 32, None, "127.0.0.1:2398")
    plan = plan_import([evil], id_regex=r"^(a+)+$")
    assert plan.errors and time.monotonic() - started < 1.0
    long_name = SourceProfile("user_" + "1" * 5000, "b" * 32, None, "127.0.0.1:2398")
    plan = plan_import([long_name], id_regex=r"^user_(\d+)$")
    assert not plan.errors  # matching is cut to 64 characters; no hang, no error


async def test_s9_importer_blocks_an_unsafe_regex(env: Env) -> None:
    from tgpanel.apply.importer import Importer

    env.fake.files[PROFILES].data = (
        b'{"profiles": [{"name": "user_12345", "secret": "'
        + b"c" * 32
        + b'", "backend": "127.0.0.1:2398"}]}'
    )
    prev = await Importer(env.pipeline).preview(id_regex=r"^(\d+)+$")
    assert prev.blocked and any("nested" in e for e in prev.errors)


# ===================================================================================== nits


def test_nit_scrub_masks_base64url_secrets_but_not_paths_or_words() -> None:
    token = "AbCdEfGhIjKlMnOpQrStUv_-12"
    assert "[redacted]" in scrub(f"bad secret {token} in profile")
    assert token not in scrub(f"secret={token}")
    assert scrub("/etc/tgpanel/mtproxy_some_long_directory_name_here/x") == (
        "/etc/tgpanel/mtproxy_some_long_directory_name_here/x"
    )
    assert scrub("configuration_validation_failed_for_profile") == (
        "configuration_validation_failed_for_profile"
    )
    assert "[redacted]" in scrub("a" * 32)
    assert scrub("tgpanel-mtproxy@1") == "tgpanel-mtproxy@1"


def test_nit_http_get_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real_build = urllib.request.build_opener

    def spy(*handlers: Any) -> Any:
        seen.extend(handlers)
        return real_build(*handlers)

    monkeypatch.setattr(urllib.request, "build_opener", spy)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    real_mod._http_get_sync("http://127.0.0.1:9/healthz", 0.2)
    proxy = [h for h in seen if isinstance(h, urllib.request.ProxyHandler)]
    assert proxy and getattr(proxy[0], "proxies", None) == {}


async def test_nit_run_command_does_not_hang_on_orphaned_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(real_mod, "_KILL_GRACE_S", 0.3)
    started = time.monotonic()
    res = await real_mod.run_command(["/bin/sh", "-c", "sleep 4 & sleep 4"], timeout_s=0.3)
    assert res.timed_out and time.monotonic() - started < 3.0


async def test_nit_sensitive_settings_are_not_audited(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tgpanel.apply.settings_spec import SPECS
    from tgpanel.services.settings_service import SettingsServiceImpl

    monkeypatch.setitem(SPECS, "panel_hostname", replace(SPECS["panel_hostname"], sensitive=True))
    svc = SettingsServiceImpl(env.pipeline, env.db)
    assert (await svc.set("panel_hostname", "hidden.example.com", "web:admin")).ok
    assert (await svc.set("timezone", "Europe/Moscow", "web:admin")).ok
    audit = env.audit_text()
    assert "settings.set|panel_hostname|***" in audit
    assert "hidden.example.com" not in audit
    assert "settings.set|timezone|Europe/Moscow" in audit


_ = add_users
