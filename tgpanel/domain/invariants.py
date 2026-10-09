"""Desired-state invariants and relay limit computation (PLAN 3.3, 3.6, 3.7)."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping

from tgpanel.domain.addresses import is_valid_loopback_ip
from tgpanel.domain.models import DesiredState, UserStatus
from tgpanel.domain.pools import (
    CLIENT_PORT_BASE,
    MAX_SECRETS_PER_PROCESS,
    MIN_SECRETS_PER_PROCESS,
    POOL_LIMIT,
    STATS_PORT_BASE,
    sentinel_host_pool,
)
from tgpanel.domain.secrets_ import base_secret, is_valid_secret

PROFILE_NAME_RE = re.compile(r"u[1-9][0-9]*")
MIN_MAX_PROFILES = 32


def compute_max_profiles(profile_count: int) -> int:
    """max(32, profiles + 16), where profiles = profiles actually rendered."""
    return max(MIN_MAX_PROFILES, profile_count + 16)


def rendered_profile_count(state: DesiredState) -> int:
    """Active users plus unmanaged profiles, or 1 (the sentinel) when there are none."""
    active = sum(1 for u in state.users if u.status is UserStatus.ACTIVE)
    total = active + len(state.foreign_profiles)
    return total if total else 1


# Upstream relay constants (internal/session/session.go, internal/config/config.go).
RELAY_DEFAULT_STREAMS_PER_SESSION = 128
RELAY_DEFAULT_PENDING_GLOBAL = 512 * 1024 * 1024
RELAY_DEFAULT_PENDING_ITEMS_GLOBAL = 256 * 1024
_CONTROL_RESERVE_EXTRA_ITEMS = 16
_CONTROL_RESERVE_ITEMS_PER_STREAM = 3
_QUEUE_ITEM_COST = 256
_FRAME_HEADER_SIZE = 8
_DATA_HEADROOM = 2  # keep at least as much room for data as the control reserve takes


def control_reserve(streams_per_session: int) -> tuple[int, int]:
    """(bytes, items) the relay reserves per session for control frames."""
    items = _CONTROL_RESERVE_EXTRA_ITEMS + streams_per_session * _CONTROL_RESERVE_ITEMS_PER_STREAM
    return items * (_QUEUE_ITEM_COST + _FRAME_HEADER_SIZE + 4), items


def required_pending_bytes(sessions: int, streams_per_session: int = 128) -> int:
    """max_pending_global the relay needs for ``sessions`` (control reserve plus data headroom)."""
    return _DATA_HEADROOM * control_reserve(streams_per_session)[0] * sessions


def compute_relay_limits(
    state: DesiredState,
    profile_count: int,
    existing: Mapping[str, object] | None = None,
) -> dict[str, int]:
    """`limits` keys to write into config.json. Never contains per-profile limits.

    ``profile_count`` = number of profiles actually rendered (disabled users do not count).
    ``existing`` = the current ``limits`` object of config.json: the relay refuses to start when
    the per-session control reserve times ``max_sessions_global`` exhausts ``max_pending_global``
    or ``max_pending_items_global``, so those two are raised (never lowered) when needed.
    """
    existing = existing or {}
    sessions = state.relay_limits.max_sessions_global
    limits = {
        "max_profiles": compute_max_profiles(profile_count),
        "max_sessions_global": sessions,
        "new_sessions_burst": sessions,
        "max_bootstraps_global": sessions,
        "new_bootstraps_burst": sessions,
        "max_streams_global": state.relay_limits.max_streams_global,
    }
    streams = _int_or(existing.get("max_streams_per_session"), RELAY_DEFAULT_STREAMS_PER_SESSION)
    reserve_bytes, reserve_items = control_reserve(streams)
    cur_bytes = _int_or(existing.get("max_pending_global"), RELAY_DEFAULT_PENDING_GLOBAL)
    cur_items = _int_or(
        existing.get("max_pending_items_global"), RELAY_DEFAULT_PENDING_ITEMS_GLOBAL
    )
    need_bytes = _DATA_HEADROOM * reserve_bytes * sessions
    need_items = _DATA_HEADROOM * reserve_items * sessions
    if need_bytes > cur_bytes:
        limits["max_pending_global"] = need_bytes
    if need_items > cur_items:
        limits["max_pending_items_global"] = need_items
    return limits


def _int_or(value: object, default: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
    )


def _dups(values: list[str]) -> list[str]:
    return [v for v, n in Counter(values).items() if n > 1]


def _foreign_names(state: DesiredState, errors: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in state.foreign_profiles:
        try:
            entry = json.loads(raw)
        except ValueError:
            errors.append("unmanaged profile is not valid JSON")
            continue
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            errors.append("unmanaged profile has no name")
            continue
        if name in names:
            errors.append("duplicate unmanaged profile name")
        names.add(name)
    return names


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
        if p.port - CLIENT_PORT_BASE != p.stats_port - STATS_PORT_BASE:
            errors.append(f"pool {p.id}: port {p.port} and stats port {p.stats_port} mismatch")
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
    foreign_names = _foreign_names(state, errors)
    ours = {u.profile_name for u in state.users} | {"_tgpanel_sentinel"}
    for name in sorted(foreign_names & ours):
        errors.append(f"unmanaged profile {name} collides with a managed profile name")
    if active == 0 and not state.foreign_profiles:
        if not is_valid_secret(state.sentinel_secret):
            errors.append("no active users and sentinel secret is missing")
        if not state.pools:
            errors.append("no active users and no pool for the sentinel profile")
        elif sentinel_host_pool(state.pools, state.users, spp) is None:
            errors.append("no active users and no managed pool has a free slot for the sentinel")
    total = rendered_profile_count(state)
    max_profiles = compute_relay_limits(state, total)["max_profiles"]
    if not 1 <= total <= max_profiles:
        errors.append(f"profile count {total} outside 1..{max_profiles}")
    return errors


def capacity_warnings(state: DesiredState) -> list[str]:
    """Non-blocking capacity hints (PLAN 3.7): pool -C budget vs max_streams_global."""
    managed = sum(1 for p in state.pools if p.managed)
    capacity = state.mtp_max_connections * managed
    if managed and capacity < state.relay_limits.max_streams_global:
        return [
            f"sum of pool max connections {capacity} is below max_streams_global "
            f"{state.relay_limits.max_streams_global}"
        ]
    return []
