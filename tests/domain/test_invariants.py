from dataclasses import replace

from tests.domain.helpers import make_pool, make_state, make_user, secret_for
from tgpanel.domain.invariants import (
    compute_max_profiles,
    compute_relay_limits,
    validate_desired_state,
)
from tgpanel.domain.models import RelayLimits, UserStatus


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
    limits = compute_relay_limits(state)
    assert set(limits) == {
        "max_profiles",
        "max_sessions_global",
        "new_sessions_burst",
        "max_bootstraps_global",
        "new_bootstraps_burst",
        "max_streams_global",
    }
    assert limits["max_profiles"] == 32
    assert limits["max_sessions_global"] == 2000
    for k in ("new_sessions_burst", "max_bootstraps_global", "new_bootstraps_burst"):
        assert limits[k] >= limits["max_sessions_global"]
    assert limits["max_streams_global"] == 32768
    big = make_state(
        [make_user(i, 1 + i // 16, ip=f"127.64.{i // 250}.{i % 250 + 1}") for i in range(1, 301)]
    )
    assert compute_relay_limits(big)["max_profiles"] == 316
