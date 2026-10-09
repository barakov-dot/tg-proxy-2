from dataclasses import replace

from tests.domain.helpers import make_pool, make_state, make_user, secret_for
from tgpanel.domain.invariants import (
    capacity_warnings,
    compute_max_profiles,
    compute_relay_limits,
    rendered_profile_count,
    validate_desired_state,
)
from tgpanel.domain.models import PoolRecord, RelayLimits, UserStatus


def test_valid_state() -> None:
    state = make_state([make_user(i) for i in range(1, 6)])
    assert validate_desired_state(state) == []


def test_duplicates_detected() -> None:
    u1 = make_user(1)
    cases = [
        make_user(2, name=u1.name),
        make_user(2, ip=u1.loopback_ip),
        make_user(2, secret=u1.secret),
        make_user(2, secret="dd" + u1.secret),  # same base secret
    ]
    for u2 in cases:
        assert validate_desired_state(make_state([u1, u2])), u2


def test_errors_never_leak_secrets() -> None:
    u1 = make_user(1)
    errs = validate_desired_state(make_state([u1, make_user(2, secret=u1.secret)]))
    assert errs and all(u1.secret not in e for e in errs)


def test_pool_capacity_and_missing_pool() -> None:
    users = [make_user(i, 1) for i in range(1, 18)]
    assert any("exceed" in e for e in validate_desired_state(make_state(users)))
    assert validate_desired_state(make_state(users[:16])) == []
    state15 = make_state(users[:16], secrets_per_process=15)
    assert any("exceed" in e for e in validate_desired_state(state15))
    assert any("does not exist" in e for e in validate_desired_state(make_state([make_user(1, 7)])))


def test_disabled_users_count_toward_capacity() -> None:
    users = [make_user(i, 1, UserStatus.DISABLED) for i in range(1, 18)]
    users.append(make_user(99, 2))
    state = make_state(users, [make_pool(0), make_pool(1)])
    assert any("exceed" in e for e in validate_desired_state(state))


def test_ip_range() -> None:
    for ip in ("127.0.0.5", "127.64.1.0", "127.64.1.255", "192.168.0.1"):
        assert validate_desired_state(make_state([make_user(1, ip=ip)]))


def test_secret_format_and_spp_range() -> None:
    assert validate_desired_state(make_state([make_user(1, secret="zz")]))
    assert validate_desired_state(make_state([make_user(1)], secrets_per_process=17))
    assert validate_desired_state(make_state([make_user(1)], secrets_per_process=0))


def test_no_active_users_needs_sentinel_and_pool() -> None:
    off = [make_user(1, status=UserStatus.DISABLED)]
    assert validate_desired_state(make_state(off)) == []
    assert validate_desired_state(make_state([])) == []
    assert validate_desired_state(make_state(off, pools=[]))
    bad = replace(make_state(off), sentinel_secret="")
    assert validate_desired_state(bad)


def test_sentinel_secret_must_not_collide() -> None:
    u = make_user(1, status=UserStatus.DISABLED, secret=secret_for(999_999))
    assert validate_desired_state(make_state([u]))


def test_max_profiles() -> None:
    assert compute_max_profiles(0) == 32
    assert compute_max_profiles(16) == 32
    assert compute_max_profiles(17) == 33
    assert compute_max_profiles(300) == 316


def test_relay_limits_keys_and_bursts() -> None:
    state = make_state([make_user(1)], relay_limits=RelayLimits(2000, 32768))
    limits = compute_relay_limits(state, 1)
    assert set(limits) == {
        "max_profiles",
        "max_sessions_global",
        "new_sessions_burst",
        "max_bootstraps_global",
        "new_bootstraps_burst",
        "max_streams_global",
        "max_pending_items_global",  # 2000 sessions exceed the default item budget
    }
    assert limits["max_profiles"] == 32
    assert limits["max_sessions_global"] == 2000
    for k in ("new_sessions_burst", "max_bootstraps_global", "new_bootstraps_burst"):
        assert limits[k] >= limits["max_sessions_global"]
    assert limits["max_streams_global"] == 32768
    big = make_state(
        [make_user(i, 1 + i // 16, ip=f"127.64.{i // 250}.{i % 250 + 1}") for i in range(1, 301)]
    )
    assert compute_relay_limits(big, 300)["max_profiles"] == 316


def test_disabled_users_do_not_inflate_max_profiles() -> None:
    users = [make_user(i, 1 + i // 16, UserStatus.DISABLED) for i in range(1, 101)]
    users.append(make_user(200, 20))
    pools = [make_pool(n) for n in range(20)]
    state = make_state(users, pools)
    assert compute_relay_limits(state, rendered_profile_count(state))["max_profiles"] == 32
    assert rendered_profile_count(make_state([users[0]])) == 1  # sentinel only


def test_stream_capacity_is_a_warning_not_an_error() -> None:
    state = make_state([make_user(1)], relay_limits=RelayLimits(1024, 16384))
    assert validate_desired_state(state) == []  # warning only, never blocks apply
    assert any("max connections" in w for w in capacity_warnings(state))
    pools = [make_pool(n) for n in range(4)]
    assert capacity_warnings(make_state([make_user(1)], pools, relay_limits=RelayLimits())) == []


def test_pool_port_pairs_must_match() -> None:
    bad = PoolRecord(1, 2401, 8900)
    assert any("mismatch" in e for e in validate_desired_state(make_state([make_user(1)], [bad])))


def test_uppercase_and_dd_secrets_compare_by_lowercase_base() -> None:
    u1 = make_user(1)
    u2 = make_user(2, secret="DD" + u1.secret.upper())
    assert any("duplicate secrets" in e for e in validate_desired_state(make_state([u1, u2])))
    assert u2.mtproxy_secret == u1.secret
    sent = replace(make_state([u1]), sentinel_secret=u1.secret.upper())
    assert any("duplicate secrets" in e for e in validate_desired_state(sent))


def test_secrets_not_in_repr() -> None:
    u = make_user(1)
    state = make_state([u])
    assert u.secret not in repr(u)
    assert state.sentinel_secret not in repr(state)


def _upstream_validate_budget(limits: dict[str, int], streams_per_session: int = 128) -> bool:
    """Mirror of tproxy-server internal/session ValidateBudget (reserve vs global pending)."""
    from tgpanel.domain.invariants import control_reserve

    cost, items = control_reserve(streams_per_session)
    sessions = limits["max_sessions_global"]
    return (
        cost <= limits.get("max_pending_global", 512 * 1024 * 1024) // sessions
        and items <= limits.get("max_pending_items_global", 256 * 1024) // sessions
    )


def test_relay_budget_passes_upstream_validation_for_any_session_count() -> None:
    for sessions in (128, 655, 1024, 4096):
        state = make_state([make_user(1)], relay_limits=RelayLimits(sessions, 16384))
        assert _upstream_validate_budget(compute_relay_limits(state, 1)), sessions


def test_default_sessions_do_not_touch_pending_limits_but_1024_raises_items() -> None:
    small = compute_relay_limits(make_state([make_user(1)], relay_limits=RelayLimits(128)), 1)
    assert "max_pending_items_global" not in small
    big = compute_relay_limits(make_state([make_user(1)], relay_limits=RelayLimits(1024)), 1)
    assert big["max_pending_items_global"] >= 2 * 400 * 1024  # 16 + 3*128 items per session


def test_existing_pending_limits_are_never_lowered_and_streams_per_session_is_respected() -> None:
    state = make_state([make_user(1)], relay_limits=RelayLimits(1024))
    existing = {"max_pending_items_global": 10_000_000, "max_streams_per_session": 256}
    limits = compute_relay_limits(state, 1, existing)
    assert "max_pending_items_global" not in limits  # already large enough
    assert _upstream_validate_budget({**existing, **limits}, 256)
