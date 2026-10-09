from __future__ import annotations

import io
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.apply.conftest import CONFIG, PROFILES, Env, add_users
from tgpanel.apply import backup as bk
from tgpanel.apply.config import ApplyPaths
from tgpanel.db import repo
from tgpanel.db.repo import BackupRecord


def members(env: Env, path: str) -> dict[str, bytes]:
    return env.fake.get_tar(path) if hasattr(env.fake, "get_tar") else _read(env, path)


def _read(env: Env, path: str) -> dict[str, bytes]:
    from tgpanel.system.validation import parse_tar_gz

    return parse_tar_gz(env.fake.files[path].data)


async def test_backup_contents_and_mode(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="create user")
    run = env.db.call(repo.get_apply_run, out.apply_run_id)
    assert run and run.backup_path
    assert env.fake.files[run.backup_path].mode == 0o600
    assert run.backup_path.startswith("/var/backups/tgpanel/2026")
    assert run.backup_path.endswith("-create-user.tar.gz")
    names = set(_read(env, run.backup_path))
    expected = {
        "etc/tproxy-server/config.json",
        "etc/tproxy-server/profiles.json",
        "etc/mtproxy/mtproxy.env",
        "etc/caddy/Caddyfile",
        "etc/tgpanel/mtproxy/1.env",
        "etc/tgpanel/tgpanel.nft",
        "etc/systemd/system/tgpanel-mtproxy@.service",
        "tgpanel.db",
        "MANIFEST.json",
    }
    assert expected <= names
    assert env.db.call(repo.list_backups)[0].path == run.backup_path


async def test_backup_holds_the_pre_operation_state(env: Env) -> None:
    before = env.fake.files[PROFILES].data
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")
    run = env.db.call(repo.get_apply_run, out.apply_run_id)
    assert run and run.backup_path
    archive = _read(env, run.backup_path)
    assert archive["etc/tproxy-server/profiles.json"] == before
    snap = sqlite3.connect(":memory:")
    snap.deserialize(archive["tgpanel.db"])
    assert snap.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0  # before the mutation
    snap.close()


async def test_db_is_snapshotted_not_copied(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    for call in env.fake.calls_of("read_file"):
        assert not str(call[1]).endswith(".db") and "/var/lib/tgpanel" not in str(call[1])


async def test_backup_does_not_log_or_leak_secrets(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")
    env.assert_no_secret_leaks()


async def test_manual_backup_is_recorded(env: Env) -> None:
    info = await env.pipeline.create_backup("Pre Install!", "system")
    assert info.reason == "pre-install"
    assert info.path.endswith("-pre-install.tar.gz")
    assert info.path in {r.path for r in env.db.call(repo.list_backups)}
    assert "backup.create" in env.audit_text()


async def test_same_second_backups_get_distinct_names(env: Env) -> None:
    frozen = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    paths = ApplyPaths()
    a = await bk.create_backup(env.fake, paths, reason="x", now=frozen, db_file=None)
    b = await bk.create_backup(env.fake, paths, reason="x", now=frozen, db_file=None)
    assert a.path != b.path and b.path.endswith("-x.2.tar.gz")


def _rec(i: int, when: datetime, reason: str = "apply") -> BackupRecord:
    return BackupRecord(i, f"/b/{i}.tar.gz", when, reason, 1)


def test_retention_last_n_plus_one_per_day() -> None:
    now = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    records = [_rec(i, now - timedelta(hours=i)) for i in range(24 * 40)]  # hourly, 40 days
    records.append(_rec(9000, now - timedelta(days=200), "pre-install"))
    doomed = {r.id for r in bk.select_prunable(records, now, keep_last=50, keep_days=30)}
    kept = [r for r in records if r.id not in doomed]
    assert 9000 not in doomed  # pre-install is never rotated
    newest = sorted(records, key=lambda r: r.created_at, reverse=True)[:50]
    assert all(r.id not in doomed for r in newest)
    # exactly one survivor per day beyond the last 50, for 30 days
    recent = [
        r
        for r in kept
        if r.created_at >= now - timedelta(days=30) and r.id not in {n.id for n in newest}
    ]
    days = [r.created_at.date() for r in recent]
    assert len(days) == len(set(days))
    old = [r for r in kept if r.created_at < now - timedelta(days=30) and r.reason != "pre-install"]
    assert old == []
    # per-day survivor is the newest backup of that day
    day = (now - timedelta(days=10)).date()
    survivors = [r for r in kept if r.created_at.date() == day]
    assert survivors and survivors[0].created_at == max(
        r.created_at for r in records if r.created_at.date() == day
    )


def test_retention_keeps_everything_when_few() -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC)
    records = [_rec(i, now - timedelta(minutes=i)) for i in range(10)]
    assert bk.select_prunable(records, now, 50, 30) == []


async def test_pipeline_prunes_old_backups(env: Env) -> None:
    env.set_setting("backup_keep_last", "2")
    env.set_setting("backup_keep_days", "0")
    await env.pipeline.create_backup("pre-install", "system")
    for _ in range(5):
        await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    records = env.db.call(repo.list_backups)
    reasons = [r.reason for r in records]
    assert reasons.count("pre-install") == 1
    assert len(records) == 3  # 2 newest + pre-install
    for r in records:
        assert r.path in env.fake.files
    on_disk = [p for p in env.fake.files if p.startswith("/var/backups/")]
    assert len(on_disk) == 3


async def test_sync_backups_registers_installer_backup(env: Env) -> None:
    env.fake.put_file("/var/backups/tgpanel/20260101T000000Z-pre-install.tar.gz", b"x", mode=0o600)
    assert await env.pipeline.sync_backups() == 1
    assert await env.pipeline.sync_backups() == 0
    assert any(r.reason == "pre-install" for r in env.db.call(repo.list_backups))


async def test_read_archive_rejects_foreign_tarballs(env: Env) -> None:
    buf = io.BytesIO()
    import gzip

    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        info = tarfile.TarInfo("hello.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    env.fake.put_file("/srv/fixture/x.tar.gz", buf.getvalue())
    try:
        await bk.read_archive(env.fake, "/srv/fixture/x.tar.gz")
    except bk.BackupError as exc:
        assert "MANIFEST" in str(exc)
    else:
        raise AssertionError("expected BackupError")


_ = (CONFIG, Path)
