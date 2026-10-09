from __future__ import annotations

import json

import pytest

from tests.render.conftest import SENTINEL, fixture_bytes, make_state, make_user, secret
from tgpanel.domain.models import PoolRecord, RelayLimits, UserStatus
from tgpanel.render.bundle import render_all
from tgpanel.render.errors import RenderError
from tgpanel.render.pools import MtproxyFacts

FACTS = MtproxyFacts(
    "/opt/MTProxy/objs/bin/mtproto-proxy",
    "mtproxy",
    "/etc/mtproxy/proxy-secret",
    "/etc/mtproxy/proxy-multi.conf",
    "--nat-info 10.0.0.5:203.0.113.5",
)


def test_render_all_deterministic_and_complete() -> None:
    users = tuple(make_user(i, pool_id=1 if i <= 16 else 2) for i in range(1, 19))
    state = make_state(users, relay_limits=RelayLimits(2048, 20000))
    cfg = fixture_bytes("clean", "config.json")
    a = render_all(state, cfg, FACTS)
    assert a == render_all(state, cfg, FACTS)
    assert a.pools_to_run == (1, 2) and a.pools_to_stop == ()
    assert a.pool_envs[1].decode().count("-S ") == 16
    assert a.pool_envs[2].decode().count("-S ") == 2
    assert b"--nat-info 10.0.0.5:203.0.113.5" in a.pool_envs[1]
    limits = json.loads(a.config_json)["limits"]
    assert limits["max_profiles"] == 34
    assert limits["max_sessions_global"] == 2048 and limits["new_sessions_burst"] == 2048
    assert limits["new_bootstraps_burst"] == 2048 and limits["max_bootstraps_global"] == 2048
    assert limits["max_streams_global"] == 20000
    assert limits["max_pending_global"] == 536870912
    assert json.loads(a.config_json)["public_dir"] == "/srv/tproxy-site"


def test_pool_without_secrets_is_stopped() -> None:
    state = make_state((make_user(1),))
    r = render_all(state, b"{}", FACTS)
    assert r.pools_to_run == (1,) and r.pools_to_stop == (2,)


def test_sentinel_in_profiles_and_pool_env() -> None:
    state = make_state((make_user(1, status=UserStatus.DISABLED),))
    r = render_all(state, b"{}", FACTS)
    assert json.loads(r.profiles_json)["profiles"][0]["secret"] == SENTINEL
    assert f"-S {SENTINEL}" in r.pool_envs[1].decode()
    assert json.loads(r.config_json)["limits"]["max_profiles"] == 32


def test_dd_secret_profile_vs_env() -> None:
    state = make_state((make_user(1, sec="dd" + secret(1)),))
    r = render_all(state, b"{}", FACTS)
    assert json.loads(r.profiles_json)["profiles"][0]["secret"] == "dd" + secret(1)
    env = r.pool_envs[1].decode()
    assert f"-S {secret(1)}" in env and "dd" + secret(1) not in env


def test_secrets_per_process_enforced() -> None:
    users = tuple(make_user(i) for i in range(1, 17))
    with pytest.raises(RenderError):
        render_all(make_state(users, secrets_per_process=15), b"{}", FACTS)
    render_all(make_state(users, secrets_per_process=16), b"{}", FACTS)
    with pytest.raises(RenderError):
        render_all(make_state(users, secrets_per_process=17), b"{}", FACTS)


def test_unmanaged_pool_ignored() -> None:
    state = make_state(
        (make_user(1, pool_id=1),),
        pools=(PoolRecord(1, 2400, 8900), PoolRecord(5, 2398, 8888, managed=False)),
    )
    r = render_all(state, b"{}", FACTS)
    assert r.pools_to_run == (1,) and r.pools_to_stop == ()


def test_invalid_config_propagates() -> None:
    with pytest.raises(RenderError):
        render_all(make_state((make_user(1),)), b"[]", FACTS)


@pytest.mark.parametrize("spp", [15, 16])
def test_full_pool_zero_active_users_is_render_error(spp: int) -> None:
    users = tuple(make_user(i, status=UserStatus.DISABLED) for i in range(1, spp + 1))
    one_pool = (PoolRecord(1, 2400, 8900),)
    with pytest.raises(RenderError):
        render_all(make_state(users, one_pool, secrets_per_process=spp), b"{}", FACTS)
    # a second pool with a free slot hosts the sentinel; no pool exceeds spp
    two = (*one_pool, PoolRecord(2, 2401, 8901))
    r = render_all(make_state(users, two, secrets_per_process=spp), b"{}", FACTS)
    assert r.pool_envs[1].decode().count("-S ") == spp
    assert r.pool_envs[2].decode().count("-S ") == 1


def test_disabled_users_do_not_inflate_max_profiles() -> None:
    users = tuple(make_user(i, status=UserStatus.DISABLED) for i in range(1, 16))
    users += (make_user(20, pool_id=2),)
    r = render_all(make_state(users), b"{}", FACTS)
    assert json.loads(r.config_json)["limits"]["max_profiles"] == 32
