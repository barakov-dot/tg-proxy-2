"""Render and parse the relay's ``profiles.json``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from tgpanel.domain.addresses import is_valid_loopback_ip
from tgpanel.domain.models import DesiredState, PoolRecord, UserRecord, UserStatus
from tgpanel.domain.pools import sentinel_host_pool, sentinel_slot_needed
from tgpanel.render.errors import RenderError

SENTINEL_NAME = "_tgpanel_sentinel"


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    name: str
    secret: str = field(repr=False)
    backend: str
    carrier_mode: str | None
    has_limits: bool


def active_users(state: DesiredState) -> list[UserRecord]:
    return sorted((u for u in state.users if u.status is UserStatus.ACTIVE), key=lambda u: u.id)


def pool_by_id(state: DesiredState, pool_id: int) -> PoolRecord:
    for pool in state.pools:
        if pool.id == pool_id:
            return pool
    raise RenderError(f"pool {pool_id} is not defined")


def sentinel_pool(state: DesiredState) -> PoolRecord:
    """Pool hosting the sentinel secret: first managed pool with a free slot (never over spp).

    The caller must create a pool beforehand (``ensure_sentinel_capacity``) if none is free.
    """
    if not any(p.managed for p in state.pools):
        raise RenderError("no managed pools: cannot render the sentinel profile")
    pool = sentinel_host_pool(state.pools, state.users, state.secrets_per_process)
    if pool is None:
        raise RenderError("no managed pool has a free slot for the sentinel profile")
    return pool


def needs_sentinel(state: DesiredState) -> bool:
    return sentinel_slot_needed(state.users) and not state.foreign_profiles


def foreign_loopback_ips(state: DesiredState) -> list[str]:
    """127.64.x.y addresses used by unmanaged profiles (kept in the accounting sets)."""
    out: list[str] = []
    for raw in state.foreign_profiles:
        backend = json.loads(raw).get("backend")
        host = backend.rpartition(":")[0] if isinstance(backend, str) else ""
        if is_valid_loopback_ip(host):
            out.append(host)
    return out


def render_profiles(state: DesiredState) -> bytes:
    entries: list[dict[str, str]] = []
    for user in active_users(state):
        pool = pool_by_id(state, user.pool_id)
        mode = user.carrier_mode or state.default_carrier_mode
        entries.append(
            {
                "name": user.profile_name,
                "secret": user.secret,
                "backend": f"{user.loopback_ip}:{pool.port}",
                "carrier_mode": str(mode),
            }
        )
    foreign = [json.loads(raw) for raw in state.foreign_profiles]
    taken = {e["name"] for e in entries} | {SENTINEL_NAME}
    for entry in foreign:
        if not isinstance(entry, dict) or entry.get("name") in taken:
            raise RenderError("unmanaged profile collides with a managed profile")
    if not entries and not foreign:
        if not state.sentinel_secret:
            raise RenderError("sentinel secret is required when no user is active")
        pool = sentinel_pool(state)
        entries.append(
            {
                "name": SENTINEL_NAME,
                "secret": state.sentinel_secret,
                "backend": f"127.0.0.1:{pool.port}",
                "carrier_mode": str(state.default_carrier_mode),
            }
        )
    return (
        json.dumps({"profiles": [*entries, *foreign]}, indent=2, ensure_ascii=False) + "\n"
    ).encode()


def _load(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RenderError(f"profiles.json is not valid JSON: {exc}") from exc


def parse_profiles(data: bytes) -> list[ProfileEntry]:
    """Parse profiles.json tolerantly (unknown fields ignored)."""
    root = _load(data)
    if not isinstance(root, dict) or not isinstance(root.get("profiles"), list):
        raise RenderError("profiles.json must be an object with a 'profiles' list")
    result: list[ProfileEntry] = []
    for raw in root["profiles"]:
        if not isinstance(raw, dict):
            raise RenderError("profile entry must be an object")
        name, secret, backend = raw.get("name"), raw.get("secret"), raw.get("backend")
        if not (isinstance(name, str) and isinstance(secret, str) and isinstance(backend, str)):
            raise RenderError("profile entry needs string name, secret and backend")
        mode = raw.get("carrier_mode")
        if mode is not None and not isinstance(mode, str):
            raise RenderError("carrier_mode must be a string")
        result.append(ProfileEntry(name, secret, backend, mode, "limits" in raw))
    return result


def profiles_hash(data: bytes) -> str:
    """sha256 of the canonical form (sorted keys, compact): ignores formatting-only changes."""
    canonical = json.dumps(_load(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
