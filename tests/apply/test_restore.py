from __future__ import annotations

from tests.apply.conftest import PROFILES, RELAY, Env, add_users, assert_only_allowed_writes
from tgpanel.db import repo
from tgpanel.domain.models import PoolRecord
from tgpanel.system.validation import build_tar_gz


def _backup_of(env: Env, run_id: int | None) -> str:
    run = env.db.call(repo.get_apply_run, run_id)
    assert run and run.backup_path
    return run.backup_path


async def test_restore_round_trip(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 3), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 2), reason="b")
    state_with_3 = _backup_of(env, op2.apply_run_id)  # taken BEFORE op2: 3 users
    assert len(env.users()) == 5
    gone = {u.mtproxy_secret for u in env.users() if u.id in (4, 5)}
    relay_before = env.fake.restart_count(RELAY)

    out = await env.pipeline.restore_backup(state_with_3, "system")
    assert out.ok, out.error
    assert [u.id for u in env.users()] == [1, 2, 3]
    names = [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]
    assert names == ["u1", "u2", "u3"]
    env_body = env.fake.get_text("/etc/tgpanel/mtproxy/1.env")
    assert env_body.count("-S ") == 3 and not any(s in env_body for s in gone)
    assert set(env.fake.nft_sets[("tgpanel", "up")]) == {u.loopback_ip for u in env.users()}
    assert env.fake.restart_count(RELAY) == relay_before + 1
    assert await env.pipeline.detect_drift() is None
    # a pre-restore backup was taken (it can undo the restore)
    pre = _backup_of(env, out.apply_run_id)
    assert pre.endswith("-restore.tar.gz")
    # history survives a restore
    assert "backup.restore" in env.audit_text()
    assert len(env.runs()) >= 4

    undo = await env.pipeline.restore_backup(pre, "system")
    assert undo.ok and len(env.users()) == 5
    assert_only_allowed_writes(env.fake)
    env.assert_no_secret_leaks()


async def test_restore_removes_pools_created_after_the_backup(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")
    op2 = await env.pipeline.run_operation(
        add_users(env.clock, 1, new_pool=PoolRecord(2, 2401, 8901)), reason="b"
    )
    snap = _backup_of(env, op2.apply_run_id)
    assert "tgpanel-mtproxy@2" in env.fake.active
    out = await env.pipeline.restore_backup(snap, "system")
    assert out.ok, out.error
    assert [p.id for p in env.db.call(repo.list_pools)] == [1]
    assert "tgpanel-mtproxy@2" not in env.fake.active
    assert "/etc/tgpanel/mtproxy/2.env" not in env.fake.files


async def test_failed_restore_rolls_back_to_the_pre_restore_state(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 3), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 2), reason="b")
    snap = _backup_of(env, op2.apply_run_id)
    files_before = env.files()
    tables_before = env.tables()
    env.fake.fail_on("systemctl", f"restart {RELAY}")
    out = await env.pipeline.restore_backup(snap, "system")
    assert not out.ok and out.rolled_back and out.rollback_errors == ()
    assert env.files() == files_before
    assert env.tables() == tables_before
    assert len(env.users()) == 5
    assert env.runs()[0].status == "failed"


async def test_restore_works_despite_external_change(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    snap = _backup_of(env, op2.apply_run_id)
    env.fake.files[PROFILES].data = b'{"profiles": []}'
    out = await env.pipeline.restore_backup(snap, "system")
    assert out.ok
    assert [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]] == ["u1"]


async def test_restore_rejects_archive_without_db(env: Env) -> None:
    env.fake.put_file(
        "/srv/fixture/files-only.tar.gz",
        build_tar_gz({"etc/x": b"1", "MANIFEST.json": b'{"format": 1}'}),
    )
    out = await env.pipeline.restore_backup(
        "/srv/fixture/files-only.tar.gz", "system", allow_external_path=True
    )
    assert not out.ok and "снимка" in (out.error or "")
    assert env.fake.calls_of("systemctl") == []


async def test_restore_rejects_garbage(env: Env) -> None:
    env.fake.put_file("/srv/fixture/junk.tar.gz", b"not an archive")
    out = await env.pipeline.restore_backup(
        "/srv/fixture/junk.tar.gz", "system", allow_external_path=True
    )
    assert not out.ok
    out = await env.pipeline.restore_backup(
        "/srv/fixture/missing.tar.gz", "system", allow_external_path=True
    )
    assert not out.ok


async def test_restore_rejects_snapshot_with_other_schema(env: Env) -> None:
    import sqlite3

    await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    snap = _backup_of(env, op2.apply_run_id)
    from tgpanel.system.validation import parse_tar_gz

    members = parse_tar_gz(env.fake.files[snap].data)
    mem = sqlite3.connect(":memory:")
    mem.deserialize(members["tgpanel.db"])
    mem.execute("INSERT INTO schema_version (version) VALUES (99)")
    mem.commit()
    members["tgpanel.db"] = mem.serialize()
    mem.close()
    env.fake.put_file("/srv/fixture/other.tar.gz", build_tar_gz(members))
    users_before = len(env.users())
    out = await env.pipeline.restore_backup(
        "/srv/fixture/other.tar.gz", "system", allow_external_path=True
    )
    assert not out.ok and "схемы" in (out.error or "")
    assert len(env.users()) == users_before
