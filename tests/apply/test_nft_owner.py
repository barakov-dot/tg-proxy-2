"""An IP handed to a different user must restart its counters from zero (W10)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from tests.apply.conftest import Env, add_users
from tgpanel.db import repo
from tgpanel.domain.models import UserStatus
from tgpanel.domain.secrets_ import generate_secret


def _swap_owner(env: Env, old_id: int) -> Callable[[sqlite3.Connection], int]:
    """Delete user ``old_id`` and create a new user on the SAME address in one operation."""

    def mutation(conn: sqlite3.Connection) -> int:
        old = repo.get_user(conn, old_id)
        assert old is not None
        repo.delete_users(conn, [old_id])
        return repo.insert_user(
            conn,
            name="newcomer",
            secret=generate_secret(),
            status=UserStatus.ACTIVE,
            pool_id=old.pool_id,
            loopback_ip=old.loopback_ip,
            created_at=env.clock(),
        )

    return mutation


async def test_reused_ip_gets_fresh_counters(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    assert out.ok and out.value
    a_id, keep_id = out.value
    a_ip = env.db.call(repo.get_user, a_id).loopback_ip  # type: ignore[union-attr]
    keep_ip = env.db.call(repo.get_user, keep_id).loopback_ip  # type: ignore[union-attr]
    env.fake.add_traffic(a_ip, up_bytes=777_000, up_packets=700, down_bytes=5, down_packets=1)
    env.fake.add_traffic(keep_ip, up_bytes=123_000, up_packets=100)

    res = await env.pipeline.run_operation(_swap_owner(env, a_id), reason="swap")
    assert res.ok

    for name in ("up", "down"):
        s = env.fake.nft_sets[("tgpanel", name)]
        assert (s[a_ip].bytes, s[a_ip].packets) == (0, 0)  # recreated
    assert env.fake.nft_sets[("tgpanel", "up")][keep_ip].bytes == 123_000  # untouched
    env.assert_no_secret_leaks()


async def test_unchanged_owner_keeps_counters(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    assert out.ok and out.value
    ip = env.db.call(repo.get_user, out.value[0]).loopback_ip  # type: ignore[union-attr]
    env.fake.add_traffic(ip, up_bytes=900, up_packets=9)
    out2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="more")
    assert out2.ok
    assert env.fake.nft_sets[("tgpanel", "up")][ip].bytes == 900


async def test_failed_swap_rolls_elements_back(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    assert out.ok and out.value
    a_id = out.value[0]
    ip = env.db.call(repo.get_user, a_id).loopback_ip  # type: ignore[union-attr]
    env.fake.add_traffic(ip, up_bytes=900, up_packets=9)
    env.fake.fail_on("systemctl", "restart tproxy-server", times=None)
    res = await env.pipeline.run_operation(_swap_owner(env, a_id), reason="swap")
    assert not res.ok
    assert ip in env.fake.nft_sets[("tgpanel", "up")]
    assert ip in env.fake.nft_sets[("tgpanel", "down")]
