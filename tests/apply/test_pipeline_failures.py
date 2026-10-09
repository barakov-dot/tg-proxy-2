"""Failure injection at every pipeline step: files, services, nft and DB must be restored."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from tests.apply.conftest import (
    CONFIG,
    NFT_FILE,
    POOL_UNIT,
    PROFILES,
    RELAY,
    Env,
    add_users,
)
from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.domain.models import PoolRecord
from tgpanel.system.ops import HttpResult

Mutation = Callable[[sqlite3.Connection], Any]


@dataclass
class Snapshot:
    files: dict[str, tuple[bytes, int, str, str]]
    tables: dict[str, list[tuple[Any, ...]]]
    active: set[str]
    enabled: set[str]
    nft: dict[tuple[str, str], list[str]]

    @classmethod
    def take(cls, env: Env) -> Snapshot:
        return cls(
            env.files(),
            env.tables(),
            set(env.fake.active),
            set(env.fake.enabled),
            {k: sorted(v) for k, v in env.fake.nft_sets.items()},
        )


def kitchen_sink(env: Env) -> Mutation:
    """New pool + users + config change; the old pool (sentinel only) becomes empty."""
    users = add_users(env.clock, 2, new_pool=PoolRecord(2, 2401, 8901))

    def mutation(conn: sqlite3.Connection) -> list[int]:
        ids: list[int] = users(conn)
        repo.set_setting(conn, "max_sessions_global", "2048")
        return ids

    return mutation


def restart_path(env: Env) -> Mutation:
    """One more user in the already running pool 1 (pool restart + relay restart)."""
    return add_users(env.clock, 1)


async def assert_rolled_back(
    env: Env, before: Snapshot, out: Any, *, expect_backup: bool = True
) -> None:
    assert out.ok is False and out.status == "failed"
    assert out.rolled_back is True
    assert out.rollback_errors == (), out.rollback_errors
    assert out.error and "изменения отменены" in out.error
    after = Snapshot.take(env)
    assert after.files == before.files, {
        p for p in set(after.files) | set(before.files) if after.files.get(p) != before.files.get(p)
    }
    assert after.tables == before.tables
    assert after.active == before.active
    assert after.enabled == before.enabled
    assert after.nft == before.nft
    last = env.runs()[0]
    assert last.status == "failed" and last.error and last.id == out.apply_run_id
    assert (last.backup_path is not None) is expect_backup
    assert "apply.failed" in env.audit_text()
    assert not [p for p in env.fake.files if ".tgpanel-check-" in p]
    env.assert_no_secret_leaks(out.error or "")


# ------------------------------------------------------------------- scenario A: kitchen sink

CASES_A: dict[str, Callable[[Env], None]] = {
    "write_env_pool2": lambda e: e.fake.fail_on("write_atomic", "/etc/tgpanel/mtproxy/2.env"),
    "write_nft_file": lambda e: e.fake.fail_on("write_atomic", NFT_FILE),
    "write_config": lambda e: e.fake.fail_on("write_atomic", CONFIG),
    "write_profiles": lambda e: e.fake.fail_on("write_atomic", PROFILES),
    "pool2_enable_now": lambda e: e.fake.fail_on("systemctl", "enable-now tgpanel-mtproxy@2"),
    "pool2_port_never_opens": lambda e: e.fake.set_port_open(2401, False),
    "nft_add_up": lambda e: e.fake.fail_on("nft_add_elements", "up"),
    "nft_add_down": lambda e: e.fake.fail_on("nft_add_elements", "down"),
    "relay_restart": lambda e: e.fake.fail_on("systemctl", f"restart {RELAY}"),
    "healthz_fails": lambda e: e.fake.queue_http("/healthz", [HttpResult(503)] * 3),
    "readyz_fails": lambda e: e.fake.queue_http("/readyz", [HttpResult(503)] * 2),
    "readyz_connection_error": lambda e: e.fake.queue_http("/readyz", [HttpResult(0)] * 2),
    "stop_emptied_pool": lambda e: e.fake.fail_on("systemctl", "disable-now tgpanel-mtproxy@1"),
}


@pytest.mark.parametrize("case", list(CASES_A))
async def test_failure_kitchen_sink_rolls_back(env: Env, case: str) -> None:
    before = Snapshot.take(env)
    relay_before = env.fake.restart_count(RELAY)
    CASES_A[case](env)
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out)
    # the injected fault really fired (one-shot injections and scripted answers are consumed)
    assert env.fake._injections == []
    assert all(not q for q in env.fake.http_script.values())
    # no link/value is handed out for a failed operation
    assert out.value is None
    # the relay answers again after the rollback
    assert (await env.fake.http_get("http://127.0.0.1:8081/healthz", 1)).status == 200
    if case in {"relay_restart", "healthz_fails", "readyz_fails", "readyz_connection_error"}:
        # relay was (attempted to be) restarted forward, so it is restarted again
        assert env.fake.restart_count(RELAY) >= relay_before + 1
    if case in {"healthz_fails", "readyz_fails", "stop_emptied_pool"}:
        assert env.fake.restart_count(RELAY) == relay_before + 2


async def test_failure_tar_backup_aborts_before_any_change(env: Env) -> None:
    before = Snapshot.take(env)
    env.fake.fail_on("make_tar_gz")
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out, expect_backup=False)
    assert env.fake.calls_of("systemctl") == []
    assert env.fake.calls_of("write_atomic") == []


async def test_failure_tproxy_check_rejects(env: Env) -> None:
    before = Snapshot.take(env)
    env.fake.fail_check("tproxy_check", "limits.max_pending_global too small")
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out)
    assert "проверка конфигурации" in (out.error or "")
    assert env.fake.calls_of("systemctl") == []


async def test_failure_tproxy_check_raises(env: Env) -> None:
    before = Snapshot.take(env)
    env.fake.fail_on("tproxy_check")
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out)


async def test_failure_render_error(env: Env) -> None:
    del env.fake.files["/etc/systemd/system/mtproxy.service"]
    env.fake.files.pop("/etc/systemd/system/mtproxy.service.d/nat.conf", None)
    env.db.call(repo.delete_setting, "mtproxy_facts")
    before = Snapshot.take(env)
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out, expect_backup=False)
    assert env.fake.calls_of("make_tar_gz") == []


async def test_failure_invariants_abort(env: Env) -> None:
    before = Snapshot.take(env)

    def too_many(conn: sqlite3.Connection) -> Any:
        add_users(env.clock, 17)(conn)  # 17 > secrets_per_process in one pool

    out = await env.pipeline.run_operation(too_many, reason="kitchen")
    await assert_rolled_back(env, before, out, expect_backup=False)
    assert "validate" not in (out.error or "") and "проверка конфигурации" in (out.error or "")


async def test_failure_unexpected_exception_type_still_rolls_back(env: Env) -> None:
    before = Snapshot.take(env)
    env.fake.fail_on("systemctl", f"restart {RELAY}", exc=RuntimeError("boom"))
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out)
    assert "внутренняя ошибка" in (out.error or "")


async def test_failure_commit_error_restores_files(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = Snapshot.take(env)

    async def broken_commit(self: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("tgpanel.apply.pipeline._OperationTxn.commit", broken_commit)
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    await assert_rolled_back(env, before, out)
    assert "сохранение в базе данных" in (out.error or "")


# ------------------------------------------------------------------- scenario B: pool restart


CASES_B: dict[str, Callable[[Env], None]] = {
    "pool1_restart": lambda e: e.fake.fail_on("systemctl", "restart tgpanel-mtproxy@1"),
    "pool1_port_closed": lambda e: e.fake.set_port_open(2400, False),
    "write_env_pool1": lambda e: e.fake.fail_on("write_atomic", "/etc/tgpanel/mtproxy/1.env"),
    "relay_restart": lambda e: e.fake.fail_on("systemctl", f"restart {RELAY}"),
    "nft_add": lambda e: e.fake.fail_on("nft_add_elements"),
}


@pytest.mark.parametrize("case", list(CASES_B))
async def test_failure_pool_restart_path(env: Env, case: str) -> None:
    seed = await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    assert seed.ok
    before = Snapshot.take(env)
    pool_before = env.fake.restart_count("tgpanel-mtproxy@1")
    CASES_B[case](env)
    out = await env.pipeline.run_operation(restart_path(env), reason="one more")
    await assert_rolled_back(env, before, out)
    assert env.fake._injections == []
    if case in {"pool1_restart", "pool1_port_closed", "relay_restart", "nft_add"}:
        # the pool was restarted forward (or attempted) and is restarted again on rollback
        assert env.fake.restart_count("tgpanel-mtproxy@1") >= pool_before + 1
    # profiles.json is the exact original: the new user never reached the relay
    names = [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]
    assert names == ["u1", "u2"]


# ------------------------------------------------------------------- scenario C: unit file


@pytest.mark.parametrize("target", ["write_unit", "daemon_reload", "pool_restart"])
async def test_failure_when_pool_unit_is_rewritten(env: Env, target: str) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    original = env.fake.files[POOL_UNIT]
    env.fake.files[POOL_UNIT] = type(original)(b"# tampered\n", 0o644, "root", "root")
    before = Snapshot.take(env)
    if target == "write_unit":
        env.fake.fail_on("write_atomic", POOL_UNIT)
    elif target == "daemon_reload":
        env.fake.fail_on("systemctl", "daemon-reload")
    else:
        env.fake.fail_on("systemctl", "restart tgpanel-mtproxy@1")
    out = await env.pipeline.apply_now("repair")
    await assert_rolled_back(env, before, out)
    assert env.fake.files[POOL_UNIT].data == b"# tampered\n"


async def test_unit_rewrite_success_reloads_and_restarts_pools(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    env.fake.files[POOL_UNIT].data = b"# tampered\n"
    reloads = env.fake.daemon_reloads
    pool_before = env.fake.restart_count("tgpanel-mtproxy@1")
    out = await env.pipeline.apply_now("repair")
    assert out.ok and out.status == "applied"
    assert env.fake.daemon_reloads == reloads + 1
    assert env.fake.restart_count("tgpanel-mtproxy@1") == pool_before + 1
    assert b"ExecStart=" in env.fake.files[POOL_UNIT].data


# ------------------------------------------------------------------- rejected mutations


async def test_rejected_mutation_changes_nothing_and_skips_apply(env: Env) -> None:
    before = Snapshot.take(env)
    runs = len(env.runs())

    def refuse(conn: sqlite3.Connection) -> None:
        repo.set_setting(conn, "timezone", "Europe/Moscow")
        raise OperationRejected("нельзя")

    out = await env.pipeline.run_operation(refuse, reason="x")
    assert out.ok is False and out.status == "rejected" and out.error == "нельзя"
    assert Snapshot.take(env) == before
    assert len(env.runs()) == runs
    assert env.fake.calls_of("make_tar_gz") == []


async def test_failed_operation_can_be_repeated_after_the_fault_is_gone(env: Env) -> None:
    env.fake.fail_on("systemctl", f"restart {RELAY}")
    first = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    assert not first.ok
    second = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    assert second.ok and second.status == "applied"
    assert len(env.users()) == 2


async def test_rollback_failure_is_reported(env: Env) -> None:
    """A fault during the rollback itself is surfaced, not swallowed."""
    env.fake.fail_on("systemctl", f"restart {RELAY}", times=None)
    out = await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    assert not out.ok and out.rollback_errors
    assert "откат выполнен не полностью" in (out.error or "")
    # files and DB are still restored
    assert [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]] == ["_tgpanel_sentinel"]
    assert env.users() == []


async def test_on_failure_hook_is_called(env: Env) -> None:
    seen: list[Any] = []

    async def hook(failure: Any) -> None:
        seen.append(failure)

    env.pipeline.on_failure = hook
    env.fake.fail_on("systemctl", f"restart {RELAY}")
    await env.pipeline.run_operation(kitchen_sink(env), reason="kitchen")
    assert len(seen) == 1 and seen[0].rolled_back
