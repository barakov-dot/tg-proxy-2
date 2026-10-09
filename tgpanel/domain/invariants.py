"""Desired-state invariants and relay limit computation (PLAN 3.3, 3.6, 3.7)."""

from __future__ import annotations

import re
from collections import Counter

from tgpanel.domain.addresses import is_valid_loopback_ip
from tgpanel.domain.models import DesiredState, UserStatus
from tgpanel.domain.pools import (
    CLIENT_PORT_BASE,
    MAX_SECRETS_PER_PROCESS,
    MIN_SECRETS_PER_PROCESS,
    POOL_LIMIT,
    STATS_PORT_BASE,
)
from tgpanel.domain.secrets_ import base_secret, is_valid_secret

PROFILE_NAME_RE = re.compile(r"u[1-9][0-9]*")
MIN_MAX_PROFILES = 32


def compute_max_profiles(user_count: int) -> int:
    """max(32, users + 16)."""
    return max(MIN_MAX_PROFILES, user_count + 16)


def compute_relay_limits(state: DesiredState) -> dict[str, int]:
    """`limits` keys to write into config.json. Never contains per-profile limits."""
    sessions = state.relay_limits.max_sessions_global
    return {
        "max_profiles": compute_max_profiles(len(state.users)),
        "max_sessions_global": sessions,
        "new_sessions_burst": sessions,
        "max_bootstraps_global": sessions,
        "new_bootstraps_burst": sessions,
        "max_streams_global": state.relay_limits.max_streams_global,
    }


def _dups(values: list[str]) -> list[str]:
    return [v for v, n in Counter(values).items() if n > 1]


def validate_desired_state(state: DesiredState) -> list[str]:
    """Return human-readable errors (empty list = valid). Messages never contain secrets."""
    errors: list[str] = []
    spp = state.secrets_per_process
    if not MIN_SECRETS_PER_PROCESS <= spp <= MAX_SECRETS_PER_PROCESS:
        errors.append(f"secrets_per_process {spp} out of range 1..16")

    pool_ids = [p.id for p in state.pools]
    for d in _dups([str(i) for i in pool_ids]):
        errors.append(f"duplicate pool id: {d}")
    for d in _dups([str(p.port) for p in state.pools]):
        errors.append(f"duplicate pool port: {d}")
    for d in _dups([str(p.stats_port) for p in state.pools]):
        errors.append(f"duplicate pool stats port: {d}")
    for p in state.pools:
        if not CLIENT_PORT_BASE <= p.port < CLIENT_PORT_BASE + POOL_LIMIT:
            errors.append(f"pool {p.id}: port {p.port} out of range")
        if not STATS_PORT_BASE <= p.stats_port < STATS_PORT_BASE + POOL_LIMIT:
            errors.append(f"pool {p.id}: stats port {p.stats_port} out of range")
    pools_by_id = {p.id: p for p in state.pools}

    for d in _dups([u.name for u in state.users]):
        errors.append(f"duplicate user name: {d}")
    for d in _dups([str(u.id) for u in state.users]):
        errors.append(f"duplicate user id: {d}")
    for d in _dups([u.loopback_ip for u in state.users]):
        errors.append(f"duplicate loopback ip: {d}")

    bases: list[str] = []
    for u in state.users:
        if not u.name:
            errors.append(f"user {u.id}: empty name")
        if not PROFILE_NAME_RE.fullmatch(u.profile_name):
            errors.append(f"user {u.id}: invalid profile name")
        if not is_valid_secret(u.secret):
            errors.append(f"user {u.id}: invalid secret format")
        else:
            bases.append(base_secret(u.secret))
        if not is_valid_loopback_ip(u.loopback_ip):
            errors.append(f"user {u.id}: loopback ip {u.loopback_ip} not in 127.64.0.0/16")
        if u.pool_id not in pools_by_id:
            errors.append(f"user {u.id}: pool {u.pool_id} does not exist")
    if is_valid_secret(state.sentinel_secret):
        bases.append(base_secret(state.sentinel_secret))
    elif state.sentinel_secret:
        errors.append("sentinel secret has invalid format")
    dup_count = sum(1 for n in Counter(bases).values() if n > 1)
    if dup_count:
        errors.append(f"duplicate secrets: {dup_count}")

    per_pool = Counter(u.pool_id for u in state.users)
    for pool_id, n in sorted(per_pool.items()):
        if n > spp and pool_id in pools_by_id:
            errors.append(f"pool {pool_id}: {n} secrets exceed secrets_per_process {spp}")

    active = sum(1 for u in state.users if u.status is UserStatus.ACTIVE)
    total = active
    if active == 0:
        total = 1  # sentinel profile
        if not is_valid_secret(state.sentinel_secret):
            errors.append("no active users and sentinel secret is missing")
        if not state.pools:
            errors.append("no active users and no pool for the sentinel profile")
    max_profiles = compute_max_profiles(len(state.users))
    if not 1 <= total <= max_profiles:
        errors.append(f"profile count {total} outside 1..{max_profiles}")
    return errors
