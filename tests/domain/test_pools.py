import pytest

from tests.domain.helpers import make_pool, make_user
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.domain.pools import (
    PoolError,
    PoolExhaustedError,
    allocate_pools,
    empty_pools,
    occupancy,
    pools_affected_by_removal,
)


@pytest.mark.parametrize("spp", [16, 15])
def test_first_user_creates_pool_one(spp: int) -> None:
    alloc = allocate_pools([], [], 1, spp)
    assert alloc.pool_ids == (1,)
    assert alloc.new_pools == (PoolRecord(id=1, port=2400, stats_port=8900),)


@pytest.mark.parametrize("spp", [16, 15])
def test_filling_pool_then_new_pool(spp: int) -> None:
    pools = [make_pool(0)]
    users = [make_user(i, 1) for i in range(1, spp)]  # spp-1 users: one free slot
    alloc = allocate_pools(pools, users, 1, spp)
    assert alloc.pool_ids == (1,)
    assert alloc.new_pools == ()
    users.append(make_user(spp, 1))
    alloc = allocate_pools(pools, users, 1, spp)
    assert alloc.pool_ids == (2,)
    assert alloc.new_pools == (PoolRecord(id=2, port=2401, stats_port=8901),)


def test_disabled_and_expired_users_keep_their_slot() -> None:
    pools = [make_pool(0)]
    users = [make_user(1, 1, UserStatus.DISABLED), make_user(2, 1, UserStatus.EXPIRED)]
    assert occupancy(pools, users) == {1: 2}
    alloc = allocate_pools(pools, users, 1, 2)
    assert alloc.pool_ids == (2,)


def test_deletion_frees_slot_in_first_pool() -> None:
    pools = [make_pool(0), make_pool(1)]
    users = [make_user(i, 1) for i in range(1, 16)] + [make_user(100, 2)]
    # pool 1 has 15 of 16 -> still one slot
    assert allocate_pools(pools, users, 1, 16).pool_ids == (1,)
    users.append(make_user(16, 1))
    assert allocate_pools(pools, users, 1, 16).pool_ids == (2,)
    users = [u for u in users if u.id != 3]
    assert allocate_pools(pools, users, 1, 16).pool_ids == (1,)


def test_bulk_allocation_is_deterministic_and_spans_pools() -> None:
    first = allocate_pools([], [], 40, 16)
    second = allocate_pools([], [], 40, 16)
    assert first == second
    assert first.pool_ids == (1,) * 16 + (2,) * 16 + (3,) * 8
    assert [p.port for p in first.new_pools] == [2400, 2401, 2402]


def test_bulk_fills_existing_free_slots_first() -> None:
    pools = [make_pool(0)]
    users = [make_user(i, 1) for i in range(1, 11)]
    alloc = allocate_pools(pools, users, 10, 16)
    assert alloc.pool_ids == (1,) * 6 + (2,) * 4


def test_import_layout_of_15_secrets_into_pool_one() -> None:
    alloc = allocate_pools([], [], 15, 16)
    assert alloc.pool_ids == (1,) * 15
    users = [make_user(i, 1) for i in range(1, 16)]
    nxt = allocate_pools(list(alloc.new_pools), users, 1, 16)
    assert nxt.pool_ids == (1,)  # last slot of pool 1
    nxt15 = allocate_pools(list(alloc.new_pools), users, 1, 15)
    assert nxt15.pool_ids == (2,)  # limit lowered to 15 -> pool 2


def test_lowest_free_index_reused_and_unmanaged_skipped() -> None:
    pools = [make_pool(0), PoolRecord(id=5, port=2402, stats_port=8902)]
    users = [make_user(1, 1)]
    alloc = allocate_pools(pools, users, 3, 1)
    assert alloc.pool_ids == (5, 6, 7)
    assert [p.port for p in alloc.new_pools] == [2401, 2403]
    unmanaged = [PoolRecord(id=1, port=2400, stats_port=8900, managed=False)]
    alloc = allocate_pools(unmanaged, [], 1, 16)
    assert alloc.pool_ids == (2,)
    assert alloc.new_pools[0].port == 2401


def test_exhaustion() -> None:
    pools = [make_pool(n) for n in range(64)]
    users = [make_user(i + 1, pools[i].id) for i in range(64)]
    with pytest.raises(PoolExhaustedError):
        allocate_pools(pools, users, 1, 1)
    assert allocate_pools(pools, users, 0, 1).pool_ids == ()


def test_invalid_settings() -> None:
    for bad in (0, 17, -1):
        with pytest.raises(PoolError):
            allocate_pools([], [], 1, bad)
    with pytest.raises(PoolError):
        allocate_pools([], [], -1, 16)


def test_empty_pools_and_keep() -> None:
    pools = [make_pool(0), make_pool(1), make_pool(2)]
    users = [make_user(1, 2)]
    assert [p.id for p in empty_pools(pools, users)] == [1, 3]
    assert [p.id for p in empty_pools(pools, users, keep_pool_ids=[1])] == [3]
    assert [p.id for p in empty_pools(pools, [])] == [1, 2, 3]


def test_unmanaged_pool_never_reported_empty() -> None:
    pools = [PoolRecord(id=1, port=2400, stats_port=8900, managed=False)]
    assert empty_pools(pools, []) == ()


def test_pools_affected_by_removal() -> None:
    users = [make_user(1, 2), make_user(2, 1), make_user(3, 2)]
    assert pools_affected_by_removal(users) == (1, 2)
