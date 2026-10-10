"""display_name survives backup/restore; snapshots made before the column existed still restore."""

from __future__ import annotations

import sqlite3

from tests.apply.conftest import Env, add_users
from tgpanel.db import repo
from tgpanel.system.validation import build_tar_gz, parse_tar_gz

NAME_1 = "Инга Базанова 🐉"


def _names(env: Env) -> dict[int, str]:
    return {u.id: u.display_name for u in env.users()}


def _old_schema_archive(env: Env, path: str, dest: str) -> None:
    """Rewrite the DB inside an archive into the pre-migration-2 shape (no display_name)."""
    members = parse_tar_gz(env.fake.files[path].data)
    mem = sqlite3.connect(":memory:")
    mem.deserialize(members["tgpanel.db"])
    mem.execute("ALTER TABLE users DROP COLUMN display_name")
    mem.execute("DELETE FROM schema_version WHERE version > 1")
    mem.commit()
    assert "display_name" not in {r[1] for r in mem.execute("PRAGMA table_info(users)")}
    members["tgpanel.db"] = mem.serialize()
    mem.close()
    env.fake.put_file(dest, build_tar_gz(members))


async def test_slim_snapshot_round_trips_display_names(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="a")
    env.db.call(repo.update_user, 1, display_name=NAME_1)
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    env.db.call(repo.update_user, 1, display_name="changed later")
    out = await env.pipeline.restore_backup(run.backup_path, "system")
    assert out.ok, out.error
    assert _names(env) == {1: NAME_1, 2: ""}


async def test_full_snapshot_round_trips_display_names(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="a")
    env.db.call(repo.update_user, 2, display_name="山田 太郎")
    info = await env.pipeline.create_backup("manual", "system")
    env.db.call(repo.update_user, 2, display_name="changed later")
    out = await env.pipeline.restore_backup(info.path, "system")
    assert out.ok, out.error
    assert _names(env) == {1: "", 2: "山田 太郎"}


async def test_old_full_backup_without_the_column_restores_with_empty_display_names(
    env: Env,
) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="a")
    info = await env.pipeline.create_backup("manual", "system")
    _old_schema_archive(env, info.path, "/srv/fixture/old-full.tar.gz")
    env.db.call(repo.update_user, 1, display_name="set after the backup")
    out = await env.pipeline.restore_backup(
        "/srv/fixture/old-full.tar.gz", "system", allow_external_path=True
    )
    assert out.ok, out.error
    assert [u.name for u in env.users()] == ["user 1", "user 2"]
    assert _names(env) == {1: "", 2: ""}  # defaults: the old snapshot knows no display names


async def test_old_slim_backup_without_the_column_restores(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    _old_schema_archive(env, run.backup_path, "/srv/fixture/old-slim.tar.gz")
    env.db.call(repo.update_user, 1, display_name=NAME_1)
    out = await env.pipeline.restore_backup(
        "/srv/fixture/old-slim.tar.gz", "system", allow_external_path=True
    )
    assert out.ok, out.error
    assert [u.id for u in env.users()] == [1, 2]
    assert _names(env)[2] == "" and _names(env)[1] == NAME_1  # surviving rows keep their label
