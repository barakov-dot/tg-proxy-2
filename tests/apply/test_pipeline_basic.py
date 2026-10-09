from __future__ import annotations

import json

from tests.apply.conftest import (
    CONFIG,
    NFT_FILE,
    POOL_UNIT,
    PROFILES,
    RELAY,
    Env,
    add_users,
    set_status_mut,
)
from tgpanel.db import repo
from tgpanel.domain.models import PoolRecord, UserStatus

POOL1 = "tgpanel-mtproxy@1"


async def test_baseline_writes_sentinel_and_starts_pool(env: Env) -> None:
    prof = env.fake.get_json(PROFILES)["profiles"]
    assert [p["name"] for p in prof] == ["_tgpanel_sentinel"]
    assert prof[0]["backend"] == "127.0.0.1:2400"
    assert env.fake.is_locked("/run/tgpanel/apply.lock") is False
    assert POOL1 in env.fake.active
    assert env.fake.get_text("/etc/tgpanel/mtproxy/1.env").count("-S ") == 1
    assert [r.status for r in env.runs()] == ["success"]


async def test_modes_and_owners_of_written_files(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="t")
    f = env.fake.files
    assert (f[PROFILES].mode, f[PROFILES].owner, f[PROFILES].group) == (0o400, "root", "tproxy")
    assert (f[CONFIG].mode, f[CONFIG].owner, f[CONFIG].group) == (0o640, "root", "tproxy")
    assert (f["/etc/tgpanel/mtproxy/1.env"].mode, f["/etc/tgpanel/mtproxy/1.env"].owner) == (
        0o600,
        "root",
    )
    assert f[NFT_FILE].mode == 0o600
    assert f[POOL_UNIT].mode == 0o644


async def test_create_batch_is_one_apply_and_one_relay_restart(env: Env) -> None:
    before_runs = len(env.runs())
    before = env.fake.restart_count(RELAY)
    out = await env.pipeline.run_operation(add_users(env.clock, 16), reason="batch")
    assert out.ok and out.status == "applied" and len(out.value or []) == 16
    assert len(env.runs()) == before_runs + 1
    assert env.fake.restart_count(RELAY) == before + 1
    names = [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]
    assert len(names) == 16 and "_tgpanel_sentinel" not in names
    limits = env.fake.get_json(CONFIG)["limits"]
    assert limits["max_profiles"] == 32
    assert limits["new_sessions_burst"] == limits["max_sessions_global"] == 1024


async def test_db_only_change_restarts_nothing_and_leaves_no_apply_row(env: Env) -> None:
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    assert out.ok
    env.fake.clear_calls()
    runs = len(env.runs())

    def touch(conn: object) -> str:
        repo.update_user(conn, 1, comment="hello")  # type: ignore[arg-type]
        return "done"

    res = await env.pipeline.run_operation(touch, reason="meta")
    assert res.ok and res.status == "noop" and res.value == "done"
    assert env.fake.calls_of("systemctl") == []
    assert env.fake.calls_of("write_atomic") == []
    assert env.fake.calls_of("make_tar_gz") == []
    assert len(env.runs()) == runs
    assert env.db.call(repo.get_user, 1).comment == "hello"  # type: ignore[union-attr]


async def test_noop_apply_changes_nothing(env: Env) -> None:
    res = await env.pipeline.apply_now("again")
    assert res.ok and res.status == "noop" and res.apply_run_id is None
    assert env.fake.calls_of("systemctl") == []


async def test_disable_restarts_only_relay(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 3), reason="seed")
    env.fake.clear_calls()
    pool_restarts = env.fake.restart_count(POOL1)
    relay_restarts = env.fake.restart_count(RELAY)
    res = await env.pipeline.run_operation(set_status_mut([2], UserStatus.DISABLED), reason="dis")
    assert res.ok
    assert env.fake.restart_count(POOL1) == pool_restarts
    assert env.fake.restart_count(RELAY) == relay_restarts + 1
    names = [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]]
    assert names == ["u1", "u3"]
    # secret of the disabled user stays in the pool env (slot is kept)
    assert env.fake.get_text("/etc/tgpanel/mtproxy/1.env").count("-S ") == 3


async def test_nft_elements_diffed_never_full_reload(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 3), reason="seed")
    env.fake.add_traffic("127.64.0.1", up_bytes=500, up_packets=5)
    env.fake.clear_calls()
    await env.pipeline.run_operation(set_status_mut([2], UserStatus.DISABLED), reason="dis")
    assert env.fake.calls_of("nft_load_file") == []
    assert set(env.fake.nft_sets[("tgpanel", "up")]) == {"127.64.0.1", "127.64.0.3"}
    # counters of untouched elements survive
    assert env.fake.nft_sets[("tgpanel", "up")]["127.64.0.1"].bytes == 500


async def test_nft_full_load_only_when_table_missing(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    del env.fake.nft_sets[("tgpanel", "up")]
    del env.fake.nft_sets[("tgpanel", "down")]
    env.fake.clear_calls()
    res = await env.pipeline.apply_now("repair")
    assert res.ok
    assert len(env.fake.calls_of("nft_load_file")) == 1
    assert env.fake.nft_sets[("tgpanel", "up")] is not None


async def test_pools_come_up_before_profiles_are_written(env: Env) -> None:
    pool2 = _pool(2)
    res = await env.pipeline.run_operation(
        add_users(env.clock, 1, new_pool=pool2), reason="new pool"
    )
    assert res.ok
    calls = env.fake.calls
    idx_wait = next(i for i, c in enumerate(calls) if c[0] == "wait_tcp_open" and c[2] == 2401)
    idx_prof = next(i for i, c in enumerate(calls) if c[0] == "write_atomic" and c[1] == PROFILES)
    idx_relay = next(i for i, c in enumerate(calls) if c[:3] == ("systemctl", "restart", RELAY))
    idx_pool_start = next(
        i for i, c in enumerate(calls) if c[:3] == ("systemctl", "enable-now", "tgpanel-mtproxy@2")
    )
    assert idx_pool_start < idx_wait < idx_prof < idx_relay
    assert 2401 in {p for p in range(2400, 2464) if env.fake.port_is_open(p)}


def _pool(n: int) -> PoolRecord:
    return PoolRecord(id=n, port=2399 + n, stats_port=8899 + n)


async def test_only_allowed_paths_are_written(env: Env) -> None:
    await env.pipeline.run_operation(
        add_users(env.clock, 3, new_pool=_pool(2)),
        reason="x",
    )
    await env.pipeline.run_operation(set_status_mut([1], UserStatus.DISABLED), reason="y")
    allowed_prefixes = (
        PROFILES,
        CONFIG,
        "/etc/tgpanel/",
        POOL_UNIT,
        "/var/backups/tgpanel/",
        "/etc/tproxy-server/.tgpanel-check-",
    )
    for call in env.fake.calls_of("write_atomic") + env.fake.calls_of("remove"):
        assert str(call[1]).startswith(allowed_prefixes), call
    for call in env.fake.calls_of("make_tar_gz"):
        assert str(call[1]).startswith("/var/backups/tgpanel/")
    # untouched upstream files stay byte-identical
    assert b"admin off" in env.fake.files["/etc/caddy/Caddyfile"].data
    assert env.fake.get_text("/etc/mtproxy/mtproxy.env").startswith("MTPROXY_SECRET=")


async def test_config_only_limits_changed(env: Env) -> None:
    before = json.loads(env.fake.files[CONFIG].data)
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    after = json.loads(env.fake.files[CONFIG].data)
    for key in before:
        if key != "limits":
            assert after[key] == before[key]


async def test_temp_check_files_are_removed(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not [p for p in env.fake.files if ".tgpanel-check-" in p]
    assert env.fake.calls_of("tproxy_check")
