"""Phase-2 review fixes: B1 (adoption), B3 (write lock), S1-S3, S10, S11 and the nits."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.apply.conftest import (
    CONFIG,
    NFT_FILE,
    POOL_UNIT,
    PROFILES,
    RELAY,
    Clock,
    Env,
    add_users,
    no_sleep,
)
from tests.apply.test_queue import GatedFake
from tgpanel.apply import backup as backup_mod
from tgpanel.apply.errors import DbWriteTimeout
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.domain.models import PoolRecord
from tgpanel.system.fake import FakeFile, FakeSystemOps

POOL1 = PoolRecord(1, 2400, 8900)
POOL2 = PoolRecord(2, 2401, 8901)
ENV1 = "/etc/tgpanel/mtproxy/1.env"
LOCK = "/var/lib/tgpanel/apply.lock"
JOURNAL = "/var/lib/tgpanel/apply.journal"


def _dbfile(env: Env) -> str:
    path = backup_mod.db_file_of(env.db)
    assert path is not None
    return path


def fresh_pipeline(
    env: Env, tmp_path: Path, name: str = "fresh.db"
) -> tuple[ApplyPipeline, Database]:
    """A brand-new (empty) database + pipeline over the SAME live fake server."""
    db = Database(tmp_path / name)
    db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    return ApplyPipeline(env.fake, db, env.config, clock=Clock(), sleep=no_sleep), db


# ===================================================================================== B1


async def test_b1_empty_db_over_live_install_stops_nothing(env: Env, tmp_path: Path) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 3), reason="seed")).ok
    files_before = env.files()
    active_before = set(env.fake.active)
    env.fake.clear_calls()

    pipe, db = fresh_pipeline(env, tmp_path)
    try:
        out = await pipe.apply_now("oops")
        assert not out.ok and out.status == "needs_adoption"
        assert "--adopt" in (out.error or "") and "restore" in (out.error or "")
        # a mutating operation is refused just the same
        op = await pipe.run_operation(add_users(Clock(), 1, new_pool=POOL1), reason="x")
        assert op.status == "needs_adoption"
    finally:
        pipe.close()
        db.close()
    assert env.fake.calls_of("systemctl") == []  # nothing stopped, started or restarted
    assert env.fake.calls_of("remove") == [] and env.fake.calls_of("write_atomic") == []
    assert (
        env.fake.calls_of("nft_delete_elements") == [] and env.fake.calls_of("nft_load_file") == []
    )
    assert env.files() == files_before and set(env.fake.active) == active_before
    assert ENV1 in env.fake.files and "tgpanel-mtproxy@1" in env.fake.active


async def test_b1_adopt_keeps_pools_profiles_and_accounting(env: Env, tmp_path: Path) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 3), reason="seed")).ok
    files_before = env.files()
    nft_before = {k: dict(v) for k, v in env.fake.nft_sets.items()}
    pipe, db = fresh_pipeline(env, tmp_path)
    try:
        env.fake.clear_calls()
        assert "принято" in await pipe.adopt("system")
        assert db.call(repo.get_setting, "apply.profiles_hash")
        out = await pipe.apply_now("after adopt")
        assert out.ok and out.status == "noop"  # nothing to change: orphans are left alone
        assert env.fake.calls_of("systemctl") == []
        assert env.fake.calls_of("nft_delete_elements") == []
        assert env.files() == files_before
        assert {k: dict(v) for k, v in env.fake.nft_sets.items()} == nft_before
        assert any("неизвестен базе" in w for w in out.warnings)
        # the adopted u<id> entries pass through as unmanaged profiles
        names = [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]
        assert names == ["u1", "u2", "u3"]
    finally:
        pipe.close()
        db.close()


async def test_b1_prune_orphans_is_explicit_and_exact(env: Env, tmp_path: Path) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    pipe, db = fresh_pipeline(env, tmp_path)
    try:
        await pipe.adopt("system")
        env.fake.clear_calls()
        relay = env.fake.restart_count(RELAY)
        out = await pipe.prune_orphans("system")
        assert out.ok
        assert env.fake.systemctl_calls() == [("disable-now", "tgpanel-mtproxy@1")]
        assert ENV1 not in env.fake.files and "tgpanel-mtproxy@1" not in env.fake.active
        assert env.fake.restart_count(RELAY) == relay  # profiles.json did not change
    finally:
        pipe.close()
        db.close()


async def test_b1_db_pool_that_is_empty_is_stopped_but_unknown_pool_is_not(env: Env) -> None:
    env.fake.put_file("/etc/tgpanel/mtproxy/7.env", b"MTP_PORT=2406\n", mode=0o600)
    env.fake.active.add("tgpanel-mtproxy@7")
    assert (
        await env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL2), reason="x")
    ).ok
    # pool 1 (in the DB, now empty) was stopped; pool 7 (unknown to the DB) was not touched
    assert "tgpanel-mtproxy@1" not in env.fake.active
    assert "tgpanel-mtproxy@7" in env.fake.active and "/etc/tgpanel/mtproxy/7.env" in env.fake.files
    assert [c for c in env.fake.systemctl_calls("disable-now")] == [
        ("disable-now", "tgpanel-mtproxy@1")
    ]


# ===================================================================================== B3


async def test_b3_snapshot_is_taken_before_begin_on_a_separate_connection(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[bool, Any, bool]] = []
    real = backup_mod.snapshot_database

    def spy(db: Database, db_path: str | None, dest: Path, *, slim: bool) -> None:
        seen.append((env.pipeline._txn_active, db_path, slim))
        real(db, db_path, dest, slim=slim)

    monkeypatch.setattr(backup_mod, "snapshot_database", spy)
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    assert seen == [(False, str(backup_mod.db_file_of(env.db)), True)]


async def test_b3_operation_connection_uses_synchronous_full(env: Env) -> None:
    conn = env.pipeline._txn.conn
    assert conn is not env.db.conn
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


async def test_b3_concurrent_writer_waits_for_the_lock_not_for_busy_timeout(
    make: Callable[..., Env],
) -> None:
    fake = GatedFake()
    fake.block_next = False
    env = make("clean", fake=fake)
    from tests.apply.conftest import drop_foreign

    drop_foreign(fake)
    assert (await env.pipeline.apply_now("init", force_external=True)).ok
    fake.block_next = True
    op = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()  # the operation transaction is open right now
    assert env.pipeline._txn_active

    def write(conn: sqlite3.Connection) -> str:
        repo.set_setting(conn, "timezone", "Europe/Moscow")
        return "written"

    started = time.monotonic()
    writer = asyncio.create_task(env.pipeline.db_write(write))
    await asyncio.sleep(0.05)
    assert not writer.done()  # waiting on the asyncio lock, not failing with "database is locked"
    fake.gate.set()
    assert (await op).ok
    assert await writer == "written"
    assert time.monotonic() - started < 3.0  # SQLite's busy_timeout is 10 s
    assert env.db.call(repo.get_setting, "timezone") == "Europe/Moscow"
    # the operation's own data was not affected by the writer
    assert len(env.users()) == 1


async def test_b3_db_write_times_out_with_a_clear_error(make: Callable[..., Env]) -> None:
    fake = GatedFake()
    fake.block_next = False
    env = make("clean", fake=fake)
    from tests.apply.conftest import drop_foreign

    drop_foreign(fake)
    assert (await env.pipeline.apply_now("init", force_external=True)).ok
    fake.block_next = True
    op = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()
    with pytest.raises(DbWriteTimeout, match="занята"):
        await env.pipeline.db_write(lambda conn: None, wait_s=0.05)
    fake.gate.set()
    assert (await op).ok


async def test_b3_writers_serialize_among_themselves(env: Env) -> None:
    order: list[int] = []

    def writer(conn: sqlite3.Connection, n: int) -> None:
        order.append(n)
        repo.set_setting(conn, "timezone", f"UTC{n}")

    await asyncio.gather(*(env.pipeline.db_write(writer, i) for i in range(5)))
    assert sorted(order) == list(range(5))


# ===================================================================================== S1


class Crash(BaseException):
    pass


async def _crash_mid_apply(env: Env, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[Any, ...]]:
    """Kill the apply right after profiles.json was written (no rollback, no finish)."""
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")).ok
    before = env.files()

    async def no_cleanup(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(ApplyPipeline, "_fail", no_cleanup)
    env.fake.fail_on("systemctl", f"restart {RELAY}", exc=Crash)  # type: ignore[arg-type]
    task = asyncio.create_task(
        env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL2), reason="doomed")
    )
    out = await task
    assert not out.ok
    with pytest.raises(Crash):
        await env.pipeline._driver  # type: ignore[misc]
    monkeypatch.undo()
    env.pipeline.close()  # the "process" dies: its DB transaction is rolled back by SQLite
    return before


async def test_s1_startup_recovery_restores_files_of_an_interrupted_apply(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = await _crash_mid_apply(env, monkeypatch)
    assert env.fake.get_json(PROFILES)["profiles"][-1]["name"] == "u2"  # half-applied state
    assert JOURNAL in env.fake.files and "/etc/tgpanel/mtproxy/2.env" in env.fake.files
    running = [r for r in env.runs() if r.status == "running"]
    assert len(running) == 1
    # leftovers of the dead process + an unrelated file that must survive
    env.fake.put_file("/etc/tproxy-server/.tgpanel-check-profiles.json", b"{}", mode=0o400)
    env.fake.put_file("/etc/tgpanel/mtproxy/.tgpanel-x1.tmp", b"x", mode=0o600)
    env.fake.put_file("/etc/tgpanel/keepme.txt", b"mine", mode=0o600)
    before = {**before, "/etc/tgpanel/keepme.txt": (b"mine", 0o600, "root", "root")}

    db2 = Database(_dbfile(env))
    pipe = ApplyPipeline(env.fake, db2, env.config, clock=Clock(), sleep=no_sleep)
    try:
        relay = env.fake.restart_count(RELAY)
        env.fake.clear_calls()
        report = await pipe.startup_recovery()
        assert report.cleaned_temp_files == 2
        assert report.run_ids == [running[0].id]
        assert "/etc/tproxy-server/profiles.json" in report.restored_files
        after = {k: v for k, v in env.files().items() if k != JOURNAL}
        assert after == before  # byte-identical to the pre-apply state
        assert JOURNAL not in env.fake.files
        assert "/etc/tgpanel/keepme.txt" in env.fake.files
        assert "/etc/tgpanel/mtproxy/2.env" not in env.fake.files
        assert "tgpanel-mtproxy@2" not in env.fake.active
        assert env.fake.restart_count(RELAY) == relay + 1  # exactly one relay restart
        assert env.fake.calls_of("http_get", "/healthz")
        run = db2.call(repo.get_apply_run, running[0].id)
        assert run and run.status == "failed" and "восстановлено" in (run.error or "")
        assert "apply.recovered" in "\n".join(
            a.action for a in db2.call(repo.list_audit, limit=100)
        )
        assert len(db2.call(repo.all_users)) == 1  # the DB was rolled back by SQLite itself
        # idempotent: a second start does nothing
        env.fake.clear_calls()
        again = await pipe.startup_recovery()
        assert again.run_ids == [] and again.restored_files == []
        assert env.fake.calls_of("systemctl") == []
    finally:
        pipe.close()
        db2.close()


async def test_s1_crash_before_any_write_restarts_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")).ok
    # a 'running' row + journal pointing at a backup whose files equal the current ones
    info = await env.pipeline.create_backup("manual", "system", full=False)
    run_id = env.db.call(repo.start_apply_run, env.clock(), "crashed")
    env.fake.put_file(
        JOURNAL, json.dumps({"run_id": run_id, "backup": info.path, "phase": "writing"}), mode=0o600
    )
    env.pipeline.close()
    db2 = Database(_dbfile(env))
    pipe = ApplyPipeline(env.fake, db2, env.config, clock=Clock(), sleep=no_sleep)
    try:
        relay = env.fake.restart_count(RELAY)
        env.fake.clear_calls()
        report = await pipe.startup_recovery()
        assert report.restored_files == [] and report.run_ids == [run_id]
        assert env.fake.calls_of("systemctl") == [] and env.fake.restart_count(RELAY) == relay
        assert JOURNAL not in env.fake.files
        row = db2.call(repo.get_apply_run, run_id)
        assert row and row.status == "failed"
    finally:
        pipe.close()
        db2.close()


async def test_s1_running_row_without_journal_is_just_marked_failed(env: Env) -> None:
    run_id = env.db.call(repo.start_apply_run, env.clock(), "crashed early")
    env.fake.clear_calls()
    report = await env.pipeline.startup_recovery()
    assert report.run_ids == [run_id] and env.fake.calls_of("systemctl") == []
    row = env.db.call(repo.get_apply_run, run_id)
    assert row and row.status == "failed" and row.error


async def test_s1_journal_is_written_after_the_backup_and_removed_on_success(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    calls = env.fake.calls
    i_tar = next(i for i, c in enumerate(calls) if c[0] == "make_tar_gz")
    i_journal = next(i for i, c in enumerate(calls) if c[:2] == ("write_atomic", JOURNAL))
    i_profiles = next(i for i, c in enumerate(calls) if c[:2] == ("write_atomic", PROFILES))
    assert i_tar < i_journal < i_profiles
    assert JOURNAL not in env.fake.files


async def test_s1_failed_apply_with_clean_rollback_removes_the_journal(env: Env) -> None:
    env.fake.fail_on("systemctl", f"restart {RELAY}")
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok and JOURNAL not in env.fake.files


# ===================================================================================== S2


class TamperingFake(FakeSystemOps):
    """Edits profiles.json 'externally' at a chosen moment."""

    def __init__(self) -> None:
        super().__init__()
        self.tamper_on: tuple[str, str] | None = None
        self.tampered: bytes = b""

    def _maybe(self, action: str, unit: str) -> None:
        if self.tamper_on == (action, unit):
            self.tamper_on = None
            doc = json.loads(self.files[PROFILES].data)
            doc["profiles"].append(
                {"name": "late", "secret": "d" * 32, "backend": "127.0.0.1:2398"}
            )
            self.tampered = json.dumps(doc).encode()
            self.files[PROFILES] = FakeFile(self.tampered, 0o400, "root", "tproxy")

    async def systemctl(self, action: str, unit: str) -> None:
        self._maybe(action, unit)
        await super().systemctl(action, unit)


async def _tamper_env(make: Callable[..., Env]) -> tuple[Env, TamperingFake]:
    fake = TamperingFake()
    env = make("clean", fake=fake)
    from tests.apply.conftest import drop_foreign

    drop_foreign(fake)
    assert (await env.pipeline.apply_now("init", force_external=True)).ok
    fake.clear_calls()
    return env, fake


async def test_s2_external_change_before_the_write_aborts_without_clobbering(
    make: Callable[..., Env],
) -> None:
    env, fake = await _tamper_env(make)
    relay = fake.restart_count(RELAY)
    fake.tamper_on = ("enable-now", "tgpanel-mtproxy@2")  # during the pool step, before profiles
    out = await env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL2), reason="x")
    assert not out.ok and "изменён вне панели" in (out.error or "")
    assert fake.files[PROFILES].data == fake.tampered  # the external edit survives
    assert fake.restart_count(RELAY) == relay  # the relay was never touched
    assert env.users() == []
    assert "tgpanel-mtproxy@2" not in fake.active  # our pool step was undone


async def test_s2_rollback_does_not_overwrite_a_file_changed_after_our_write(
    make: Callable[..., Env],
) -> None:
    env, fake = await _tamper_env(make)
    relay = fake.restart_count(RELAY)
    fake.tamper_on = ("restart", RELAY)
    fake.fail_on("systemctl", f"restart {RELAY}", times=1)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok
    assert fake.files[PROFILES].data == fake.tampered  # NOT clobbered by the rollback
    assert any("изменён вне панели" in e for e in out.rollback_errors)
    assert "ВНИМАНИЕ" in (out.error or "")
    assert fake.restart_count(RELAY) == relay + 1  # only the rollback restart succeeded
    # the files we did own are restored
    assert json.loads(fake.files[CONFIG].data)["limits"]["max_profiles"] == 32


async def test_s2_backup_is_built_from_the_pre_images(env: Env) -> None:
    env.fake.clear_calls()
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    calls = env.fake.calls
    i_tar = next(i for i, c in enumerate(calls) if c[0] == "make_tar_gz")
    before_tar = [c[1] for c in calls[:i_tar] if c[0] == "read_file"]
    # drift check + adoption check + plan pre-image: the backup itself reads nothing again
    assert before_tar.count(PROFILES) == 3
    assert before_tar.count(CONFIG) == 1  # plan pre-image only
    assert before_tar.count(NFT_FILE) == 1 and before_tar.count(POOL_UNIT) == 1


# ===================================================================================== S3/S4


def _drop_nft(env: Env) -> None:
    env.fake.nft_sets.pop(("tgpanel", "up"))
    env.fake.nft_sets.pop(("tgpanel", "down"))


async def test_s3_table_is_loaded_before_the_first_pool_starts(env: Env) -> None:
    _drop_nft(env)
    env.fake.clear_calls()
    assert (
        await env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL2), reason="x")
    ).ok
    calls = env.fake.calls
    i_load = next(i for i, c in enumerate(calls) if c[0] == "nft_load_file")
    i_pool = next(i for i, c in enumerate(calls) if c[:2] == ("systemctl", "enable-now"))
    assert i_load < i_pool
    assert len(env.fake.calls_of("nft_load_file")) == 1


@pytest.mark.parametrize(
    "fault",
    ["relay_restart", "pool_start", "healthz"],
)
async def test_s4_failure_after_a_full_nft_load_deletes_the_new_table(env: Env, fault: str) -> None:
    _drop_nft(env)
    if fault == "relay_restart":
        env.fake.fail_on("systemctl", f"restart {RELAY}")
    elif fault == "pool_start":
        env.fake.fail_on("systemctl", "enable-now tgpanel-mtproxy@2")
    else:
        from tgpanel.system.ops import HttpResult

        env.fake.queue_http("/healthz", [HttpResult(503)] * 3)
    env.fake.clear_calls()
    out = await env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL2), reason="x")
    assert not out.ok and out.rollback_errors == ()
    assert len(env.fake.calls_of("nft_delete_table")) == 1
    assert not [k for k in env.fake.nft_sets if k[0] == "tgpanel"]  # back to "no table"
    assert len(env.fake.calls_of("nft_load_file")) == 1  # nothing was reloaded on rollback


async def test_s4_failed_forced_reload_restores_the_previous_ruleset(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")).ok
    previous = env.fake.get_text(NFT_FILE)
    loads = len(env.fake.nft_loaded)
    env.fake.fail_on("systemctl", f"restart {RELAY}")
    env.fake.clear_calls()
    # a forced reload changes nothing in the files, but the relay restart fails -> rollback
    env.fake.files[PROFILES].data = env.fake.files[PROFILES].data.replace(b"u1", b"u1")
    out = await env.pipeline.run_operation(
        add_users(env.clock, 1), reason="x", full_nft_reload=True
    )
    assert not out.ok and out.rollback_errors == ()
    assert len(env.fake.nft_loaded) == loads + 2  # forward load + rollback reload
    assert env.fake.nft_loaded[-1] == previous  # the previous nft file content is back
    assert env.fake.calls_of("nft_delete_table") == []
    assert env.fake.get_text(NFT_FILE) == previous


# ===================================================================================== S10


async def test_s10_restore_by_id_and_registered_path(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    rec = next(r for r in env.db.call(repo.list_backups) if r.path == run.backup_path)
    by_id = await env.pipeline.restore_backup(rec.id, "web:admin")
    assert by_id.ok and len(env.users()) == 1
    # by registered path
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="c")
    assert (await env.pipeline.restore_backup(run.backup_path, "web:admin")).ok


async def test_s10_unregistered_or_foreign_archives_are_refused(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    data = env.fake.files[run.backup_path].data
    # a valid archive in the backups directory that the DB does not know
    env.fake.put_file("/var/backups/tgpanel/20300101T000000Z-evil.tar.gz", data, mode=0o600)
    # a REGISTERED row pointing outside the backups directory
    env.fake.put_file("/srv/fixture/outside.tar.gz", data, mode=0o600)
    env.db.call(repo.add_backup, "/srv/fixture/outside.tar.gz", env.clock(), "x", len(data))
    for source in (
        "/var/backups/tgpanel/20300101T000000Z-evil.tar.gz",
        "/srv/fixture/outside.tar.gz",
        "/var/backups/tgpanel/../../../srv/fixture/outside.tar.gz",
        999999,
    ):
        out = await env.pipeline.restore_backup(source, "web:admin")
        assert not out.ok and out.status == "rejected" and "зарегистрированную" in (out.error or "")
    assert len(env.users()) == 2
    # explicit CLI-style use of an external file is still possible
    ok = await env.pipeline.restore_backup(
        "/srv/fixture/outside.tar.gz", "system", allow_external_path=True
    )
    assert ok.ok


# ===================================================================================== S11


async def test_s11_poison_operation_does_not_fail_unrelated_ones(make: Callable[..., Env]) -> None:
    fake = GatedFake()
    fake.block_next = False
    env = make("clean", fake=fake)
    from tests.apply.conftest import drop_foreign

    drop_foreign(fake)
    assert (await env.pipeline.apply_now("init", force_external=True)).ok
    fake.block_next = True
    failures: list[Any] = []

    async def hook(failure: Any) -> None:
        failures.append(failure)

    env.pipeline.on_failure = hook
    first = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="A"))
    await fake.reached.wait()
    bad = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 17), reason="bad"))
    good1 = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="g1"))
    good2 = asyncio.create_task(env.pipeline.run_operation(add_users(env.clock, 1), reason="g2"))
    await asyncio.sleep(0)
    relay = fake.restart_count(RELAY)
    runs = len(env.runs())
    fake.gate.set()
    a, b, g1, g2 = await asyncio.gather(first, bad, good1, good2)
    assert a.ok and g1.ok and g2.ok
    assert not b.ok and "Групповое применение не удалось" in (b.error or "")
    assert len({g1.apply_run_id, g2.apply_run_id, b.apply_run_id}) == 3  # one apply each
    assert len(env.users()) == 3  # A + g1 + g2; the 17 bad ones left no trace
    assert fake.restart_count(RELAY) == relay + 3  # A, g1, g2 (the bad ones never restart)
    # the combined run + 3 individual runs
    assert len(env.runs()) == runs + 1 + 3
    assert len(failures) == 1  # only the final failure is announced


async def test_s11_two_ops_where_one_is_bad_single_op_batches_are_not_retried(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 17), reason="bad")
    assert not out.ok and "Групповое" not in (out.error or "")


# ===================================================================================== nits


async def test_nit_owner_and_group_changes_are_detected(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")).ok
    env.fake.files[ENV1] = FakeFile(env.fake.files[ENV1].data, 0o600, "mtproxy", "mtproxy")
    env.fake.clear_calls()
    pool = env.fake.restart_count("tgpanel-mtproxy@1")
    out = await env.pipeline.apply_now("fix owner")
    assert out.ok and out.status == "applied"
    assert (env.fake.files[ENV1].owner, env.fake.files[ENV1].group) == ("root", "root")
    assert env.fake.restart_count("tgpanel-mtproxy@1") == pool + 1


async def test_nit_emptied_pool_that_is_enabled_but_inactive_is_still_disabled(env: Env) -> None:
    def pool_only(conn: sqlite3.Connection) -> None:
        repo.insert_pool(conn, POOL2, env.clock())

    env.fake.enabled.add("tgpanel-mtproxy@2")  # enabled at boot, but currently not running
    env.fake.clear_calls()
    out = await env.pipeline.run_operation(pool_only, reason="x")
    assert out.ok
    assert env.fake.systemctl_calls("disable-now", "tgpanel-mtproxy@2") == [
        ("disable-now", "tgpanel-mtproxy@2")
    ]
    assert "tgpanel-mtproxy@2" not in env.fake.enabled


async def test_nit_errors_after_commit_never_turn_into_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ApplyPipeline) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ApplyPipeline, "_prune", broken)
    relay = env.fake.restart_count(RELAY)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.ok and out.status == "applied" and not out.rolled_back
    assert any("очистка старых бэкапов" in w for w in out.warnings)
    assert len(env.users()) == 1 and env.fake.restart_count(RELAY) == relay + 1
    assert env.runs()[0].status == "success"


async def test_nit_failing_cleanup_after_commit_is_a_warning(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")).ok
    assert (
        await env.pipeline.run_operation(
            add_users(env.clock, 1, new_pool=POOL2), reason="second pool"
        )
    ).ok
    env.fake.fail_on("remove", "/etc/tgpanel/mtproxy/2.env")  # the stale env of the emptied pool

    def drop(conn: sqlite3.Connection) -> None:
        repo.delete_users(conn, [2])

    out = await env.pipeline.run_operation(drop, reason="x")
    assert out.ok and out.status == "applied"
    assert any("остановленного пула" in w for w in out.warnings)
    assert "tgpanel-mtproxy@2" not in env.fake.active
    assert env.runs()[0].status == "success"
