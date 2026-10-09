from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.apply.conftest import (
    Clock,
    Env,
    add_users,
    assert_only_allowed_writes,
    no_sleep,
)
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import HttpResult


def count(env: Env, endpoint: str) -> int:
    return len([c for c in env.fake.calls_of("http_get") if str(c[1]).endswith(endpoint)])


async def test_exactly_one_readyz_on_success(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")
    assert out.ok
    assert count(env, "/readyz") == 1
    assert count(env, "/healthz") == 1


async def test_no_health_traffic_without_a_relay_restart(env: Env) -> None:
    def comment_only(conn: sqlite3.Connection) -> None:
        repo.set_setting(conn, "timezone", "UTC")

    await env.pipeline.run_operation(comment_only, reason="x")
    assert count(env, "/readyz") == 0 and count(env, "/healthz") == 0


async def test_transient_readyz_failure_is_retried_within_the_budget(env: Env) -> None:
    env.fake.queue_http("/readyz", [HttpResult(503)])
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.ok and count(env, "/readyz") == 2  # readyz_attempts == 2 in the fixture


async def test_readyz_is_never_polled_beyond_the_attempt_budget(env: Env) -> None:
    env.fake.set_http("/readyz", 503)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok
    # forward: 2 attempts; rollback only waits for /healthz
    assert count(env, "/readyz") == env.pipeline.config.timing.readyz_attempts


async def test_healthz_wait_is_bounded(env: Env) -> None:
    env.fake.set_http("/healthz", 503)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok and out.rollback_errors  # rollback could not confirm health either
    attempts = env.pipeline.config.timing.healthz_attempts
    assert count(env, "/healthz") == 2 * attempts
    assert count(env, "/readyz") == 0


async def test_in_memory_database_fallback_still_rolls_back(tmp_path: Path) -> None:
    fake = FakeSystemOps()
    fake.seed_upstream("clean")
    db = Database(":memory:")
    db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    pipe = ApplyPipeline(
        fake,
        db,
        ApplyConfig(
            timing=ApplyTiming(healthz_attempts=2, readyz_attempts=1, healthz_interval_s=0)
        ),
        clock=Clock(),
        sleep=no_sleep,
    )
    assert (await pipe.apply_now("init", force_external=True)).ok
    clock = Clock()
    ok = await pipe.run_operation(add_users(clock, 1), reason="x")
    assert ok.ok and len(db.call(repo.all_users)) == 1
    fake.fail_on("systemctl", "restart tproxy-server")
    bad = await pipe.run_operation(add_users(clock, 1), reason="y")
    assert not bad.ok and len(db.call(repo.all_users)) == 1
    assert (
        fake.restart_count("tproxy-server") == 3
    )  # init, ok, and the rollback restart (the injected forward restart never ran)
    pipe.close()
    db.close()


async def test_recover_interrupted_marks_running_rows(env: Env) -> None:
    run_id = env.db.call(repo.start_apply_run, env.clock(), "crashed")
    assert await env.pipeline.recover_interrupted() == [run_id]
    run = env.db.call(repo.get_apply_run, run_id)
    assert run and run.status == "failed" and run.error
    assert await env.pipeline.recover_interrupted() == []


async def test_allowed_write_set_over_a_full_lifecycle(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 3), reason="x")
    await env.pipeline.create_backup("manual", "system")
    await env.pipeline.apply_now("again")
    assert_only_allowed_writes(env.fake)
