"""Pure MTProxy pool/slot allocator (PLAN 3.2).

A user occupies a slot in ITS pool regardless of status (active/disabled/expired);
only deletion frees the slot. Pools are numbered by index ``n`` (0..63): client port
``2400 + n``, stats port ``8900 + n``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from tgpanel.domain.models import PoolRecord, UserRecord, UserStatus

POOL_LIMIT = 64
CLIENT_PORT_BASE = 2400
STATS_PORT_BASE = 8900
MIN_SECRETS_PER_PROCESS = 1
MAX_SECRETS_PER_PROCESS = 16


class PoolError(ValueError):
    """Invalid pool configuration or request."""


class PoolExhaustedError(PoolError):
    """All 64 pools exist and are full."""


@dataclass(frozen=True, slots=True)
class PoolAllocation:
    """Result of allocation: pool id per requested user (in order) and pools to create."""

    pool_ids: tuple[int, ...]
    new_pools: tuple[PoolRecord, ...]


def check_secrets_per_process(value: int) -> int:
    if not MIN_SECRETS_PER_PROCESS <= value <= MAX_SECRETS_PER_PROCESS:
        raise PoolError(
            f"secrets_per_process must be {MIN_SECRETS_PER_PROCESS}..{MAX_SECRETS_PER_PROCESS}"
        )
    return value


def pool_index(pool: PoolRecord) -> int:
    return pool.port - CLIENT_PORT_BASE


def pool_for_index(pool_id: int, index: int) -> PoolRecord:
    if not 0 <= index < POOL_LIMIT:
        raise PoolError(f"pool index out of range: {index}")
    return PoolRecord(id=pool_id, port=CLIENT_PORT_BASE + index, stats_port=STATS_PORT_BASE + index)


def occupancy(pools: Iterable[PoolRecord], users: Iterable[UserRecord]) -> dict[int, int]:
    """Number of occupied slots per pool id (every pool is present, possibly with 0)."""
    counts = {p.id: 0 for p in pools}
    for u in users:
        if u.pool_id in counts:
            counts[u.pool_id] += 1
    return counts


def allocate_pools(
    pools: Sequence[PoolRecord],
    users: Iterable[UserRecord],
    count: int,
    secrets_per_process: int,
) -> PoolAllocation:
    """Assign ``count`` new users to pools: first managed pool with free slots, else new pool.

    Deterministic: pools are scanned in id order, new pools get the lowest free index and
    id = max(existing id) + 1.
    """
    check_secrets_per_process(secrets_per_process)
    if count < 0:
        raise PoolError("count must be >= 0")
    counts = occupancy(pools, users)
    ordered = sorted((p for p in pools if p.managed), key=lambda p: p.id)
    used_indices = {pool_index(p) for p in pools}
    next_id = max((p.id for p in pools), default=0) + 1
    new_pools: list[PoolRecord] = []
    assigned: list[int] = []
    cursor = 0
    for _ in range(count):
        while cursor < len(ordered) and counts[ordered[cursor].id] >= secrets_per_process:
            cursor += 1
        if cursor == len(ordered):
            free = next((i for i in range(POOL_LIMIT) if i not in used_indices), None)
            if free is None:
                raise PoolExhaustedError("all 64 MTProxy pools are full")
            pool = pool_for_index(next_id, free)
            next_id += 1
            used_indices.add(free)
            new_pools.append(pool)
            ordered.append(pool)
            counts[pool.id] = 0
        pool = ordered[cursor]
        counts[pool.id] += 1
        assigned.append(pool.id)
    return PoolAllocation(pool_ids=tuple(assigned), new_pools=tuple(new_pools))


def sentinel_slot_needed(users: Iterable[UserRecord]) -> bool:
    """The sentinel secret occupies a pool slot exactly when no user is ACTIVE (PLAN 3.3)."""
    return not any(u.status is UserStatus.ACTIVE for u in users)


def sentinel_host_pool(
    pools: Iterable[PoolRecord], users: Iterable[UserRecord], secrets_per_process: int
) -> PoolRecord | None:
    """First managed pool (by id) with a free slot, or None. Single source of truth."""
    pools = tuple(pools)
    counts = occupancy(pools, users)
    for pool in sorted((p for p in pools if p.managed), key=lambda p: p.id):
        if counts[pool.id] < secrets_per_process:
            return pool
    return None


def ensure_sentinel_capacity(
    pools: Sequence[PoolRecord], users: Iterable[UserRecord], secrets_per_process: int
) -> PoolRecord | None:
    """New pool to create so the sentinel secret has a slot, or None if nothing is needed.

    Needed only when no user is active and no managed pool has a free slot.
    """
    check_secrets_per_process(secrets_per_process)
    users = tuple(users)
    if not sentinel_slot_needed(users):
        return None
    if sentinel_host_pool(pools, users, secrets_per_process) is not None:
        return None
    used = {pool_index(p) for p in pools}
    free = next((i for i in range(POOL_LIMIT) if i not in used), None)
    if free is None:
        raise PoolExhaustedError("all 64 MTProxy pools are full")
    return pool_for_index(max((p.id for p in pools), default=0) + 1, free)


def empty_pools(
    pools: Iterable[PoolRecord],
    users: Iterable[UserRecord],
    keep_pool_ids: Iterable[int] = (),
) -> tuple[PoolRecord, ...]:
    """Managed pools without any user (to be stopped), except ``keep_pool_ids``.

    Callers pass the sentinel pool in ``keep_pool_ids`` when no user exists at all.
    """
    pools = tuple(pools)
    counts = occupancy(pools, users)
    keep = set(keep_pool_ids)
    return tuple(p for p in pools if p.managed and counts[p.id] == 0 and p.id not in keep)


def pools_affected_by_removal(
    users_to_remove: Iterable[UserRecord],
) -> tuple[int, ...]:
    """Pool ids whose env must be rewritten (and process restarted) after deletions."""
    return tuple(sorted({u.pool_id for u in users_to_remove}))
