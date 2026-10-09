from __future__ import annotations

import sqlite3

from tests.apply.conftest import PROFILES, Env, add_users, set_status_mut
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus

ENV1 = "/etc/tgpanel/mtproxy/1.env"
ENV2 = "/etc/tgpanel/mtproxy/2.env"


def sentinel(env: Env) -> str:
    value = env.setting("sentinel_secret")
    assert value
    return value


def profile_names(env: Env) -> list[str]:
    return [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]


async def test_sentinel_when_no_users(env: Env) -> None:
    assert profile_names(env) == ["_tgpanel_sentinel"]
    assert f"-S {sentinel(env)}" in env.fake.get_text(ENV1)
    # sentinel has no loopback address: nothing in the accounting sets
    assert env.fake.nft_sets[("tgpanel", "up")] == {}


async def test_sentinel_removed_when_first_user_appears(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert profile_names(env) == ["u1"]
    assert sentinel(env) not in env.fake.get_text(ENV1)
    assert env.fake.get_text(ENV1).count("-S ") == 1


async def test_last_active_user_disabled_brings_sentinel_back(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")
    out = await env.pipeline.run_operation(set_status_mut([1, 2], UserStatus.DISABLED), reason="d")
    assert out.ok
    assert profile_names(env) == ["_tgpanel_sentinel"]
    body = env.fake.get_text(ENV1)
    assert f"-S {sentinel(env)}" in body and body.count("-S ") == 3  # users keep their slots
    prof = env.fake.get_json(PROFILES)["profiles"][0]
    assert prof["backend"] == "127.0.0.1:2400"
    assert env.fake.nft_sets[("tgpanel", "up")] == {}
    # re-enabling removes it again
    out = await env.pipeline.run_operation(set_status_mut([1], UserStatus.ACTIVE), reason="e")
    assert out.ok and profile_names(env) == ["u1"]
    assert sentinel(env) not in env.fake.get_text(ENV1)


async def test_sentinel_secret_is_stable(env: Env) -> None:
    first = sentinel(env)
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    await env.pipeline.run_operation(set_status_mut([1], UserStatus.DISABLED), reason="d")
    assert sentinel(env) == first
    assert first in env.fake.get_text(PROFILES)


async def test_full_pool_gets_a_new_pool_for_the_sentinel(env: Env) -> None:
    assert (await env.pipeline.run_operation(_set("secrets_per_process", "2"), reason="s")).ok
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")  # pool 1 is full
    out = await env.pipeline.run_operation(set_status_mut([1, 2], UserStatus.DISABLED), reason="d")
    assert out.ok, out.error
    pools = env.db.call(repo.list_pools)
    assert [(p.id, p.port) for p in pools] == [(1, 2400), (2, 2401)]
    prof = env.fake.get_json(PROFILES)["profiles"][0]
    assert prof["name"] == "_tgpanel_sentinel" and prof["backend"] == "127.0.0.1:2401"
    assert env.fake.get_text(ENV2).strip().count("-S ") == 1
    assert "tgpanel-mtproxy@2" in env.fake.active and env.fake.port_is_open(2401)
    assert env.fake.get_text(ENV1).count("-S ") == 2  # pool 1 untouched: still two users
    # first user comes back: sentinel pool is stopped and its env removed
    out = await env.pipeline.run_operation(set_status_mut([1], UserStatus.ACTIVE), reason="e")
    assert out.ok
    assert profile_names(env) == ["u1"]
    assert "tgpanel-mtproxy@2" not in env.fake.active
    assert ENV2 not in env.fake.files


async def test_deleting_all_users_restores_the_sentinel_profile(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")

    def delete_all(conn: sqlite3.Connection) -> None:
        repo.delete_users(conn, [1, 2])

    out = await env.pipeline.run_operation(delete_all, reason="del")
    assert out.ok
    assert profile_names(env) == ["_tgpanel_sentinel"]
    assert env.fake.get_text(ENV1).count("-S ") == 1


async def test_emptied_pool_is_stopped_only_after_relay_is_healthy(env: Env) -> None:
    from tgpanel.domain.models import PoolRecord

    await env.pipeline.run_operation(
        add_users(env.clock, 1, new_pool=PoolRecord(2, 2401, 8901)), reason="x"
    )
    # pool 1 held only the sentinel and is stopped; pool 2 runs the user
    assert "tgpanel-mtproxy@1" not in env.fake.active and ENV1 not in env.fake.files
    calls = env.fake.calls
    i_health = max(i for i, c in enumerate(calls) if c[0] == "http_get" and "/readyz" in c[1])
    i_stop = next(
        i for i, c in enumerate(calls) if c[:3] == ("systemctl", "disable-now", "tgpanel-mtproxy@1")
    )
    assert i_health < i_stop


def _set(key: str, value: str):  # type: ignore[no-untyped-def]
    def mutation(conn: sqlite3.Connection) -> None:
        repo.set_setting(conn, key, value)

    return mutation
