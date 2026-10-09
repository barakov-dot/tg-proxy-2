from __future__ import annotations

import pytest

from tests.domain.helpers import make_pool, make_state, make_user
from tgpanel.domain.invariants import validate_desired_state
from tgpanel.domain.models import PoolRecord, UserRecord, UserStatus
from tgpanel.domain.pools import (
    PoolExhaustedError,
    ensure_sentinel_capacity,
    sentinel_slot_needed,
)


def disabled(n: int, pool_id: int = 1) -> list[UserRecord]:
    return [make_user(i, pool_id, UserStatus.DISABLED) for i in range(1, n + 1)]


def test_slot_needed_only_without_active() -> None:
    assert sentinel_slot_needed([])
    assert sentinel_slot_needed(disabled(2))
    assert not sentinel_slot_needed([*disabled(2), make_user(9)])


@pytest.mark.parametrize("spp", [15, 16])
def test_full_pool_zero_active_is_invalid(spp: int) -> None:
    state = make_state(disabled(spp), secrets_per_process=spp)
    assert any("free slot" in e for e in validate_desired_state(state))
    assert ensure_sentinel_capacity(state.pools, state.users, spp) == PoolRecord(2, 2401, 8901)


def test_free_slot_means_no_new_pool_and_valid() -> None:
    state = make_state(disabled(14), secrets_per_process=15)
    assert ensure_sentinel_capacity(state.pools, state.users, 15) is None
    assert validate_desired_state(state) == []


def test_other_pool_with_free_slot_hosts_sentinel() -> None:
    pools = [make_pool(0), make_pool(1)]
    state = make_state(disabled(16), pools, secrets_per_process=16)
    assert ensure_sentinel_capacity(state.pools, state.users, 16) is None
    assert validate_desired_state(state) == []


def test_no_new_pool_with_active_user() -> None:
    users = [*disabled(15), make_user(50)]
    assert ensure_sentinel_capacity([make_pool(0)], users, 16) is None


def test_ensure_creates_first_pool_and_uses_lowest_free_index() -> None:
    assert ensure_sentinel_capacity([], [], 16) == PoolRecord(1, 2400, 8900)
    pools = [PoolRecord(7, 2401, 8901), make_pool(2)]
    full = [make_user(1, 7, UserStatus.DISABLED), make_user(2, 3, UserStatus.DISABLED)]
    assert ensure_sentinel_capacity(pools, full, 1) == PoolRecord(8, 2400, 8900)


def test_ensure_all_pools_exhausted() -> None:
    pools = [PoolRecord(i + 1, 2400 + i, 8900 + i) for i in range(64)]
    users = [make_user(i + 1, i + 1, UserStatus.DISABLED) for i in range(64)]
    with pytest.raises(PoolExhaustedError):
        ensure_sentinel_capacity(pools, users, 1)
