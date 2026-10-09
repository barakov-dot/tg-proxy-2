"""Build the DesiredState from the database and read MTProxy facts from the installed unit."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime

from tgpanel.apply.config import ApplyPaths
from tgpanel.apply.settings_spec import (
    KEY_MTPROXY_FACTS,
    KEY_SENTINEL_SECRET,
    AppSettings,
    read_settings,
)
from tgpanel.db import repo
from tgpanel.domain.models import DesiredState, PoolRecord, RelayLimits
from tgpanel.domain.pools import ensure_sentinel_capacity
from tgpanel.domain.secrets_ import generate_secret, is_valid_secret
from tgpanel.render.errors import RenderError
from tgpanel.render.pools import MtproxyFacts, extract_mtproxy_facts
from tgpanel.system.ops import SystemOps, SystemOpsError

_ENV_FILE_RE = re.compile(r"^\s*EnvironmentFile\s*=\s*-?\s*(/\S+)\s*$", re.MULTILINE)
_UNIT_SEARCH = ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system")


def _sentinel_secret(conn: sqlite3.Connection) -> str:
    current = repo.get_setting(conn, KEY_SENTINEL_SECRET)
    if current and is_valid_secret(current):
        return current
    fresh = generate_secret()
    repo.set_setting(conn, KEY_SENTINEL_SECRET, fresh)
    return fresh


def load_desired_state(
    conn: sqlite3.Connection, now: datetime, settings: AppSettings | None = None
) -> DesiredState:
    """Desired state from the DB (call inside the operation transaction).

    Side effects inside the caller's transaction: creates the sentinel secret once and, when no
    user is active and no managed pool has a free slot, the pool that hosts the sentinel.
    """
    cfg = settings or read_settings(conn)
    pools = repo.list_pools(conn)
    users = repo.all_users(conn)
    new_pool = ensure_sentinel_capacity(pools, users, cfg.secrets_per_process)
    if new_pool is not None:
        repo.insert_pool(conn, new_pool, now)
        pools.append(new_pool)
    return DesiredState(
        users=tuple(users),
        pools=tuple(pools),
        sentinel_secret=_sentinel_secret(conn),
        default_carrier_mode=cfg.carrier_mode_default,
        relay_limits=RelayLimits(cfg.max_sessions_global, cfg.max_streams_global),
        secrets_per_process=cfg.secrets_per_process,
        mtp_workers=cfg.mtp_workers,
        mtp_max_connections=cfg.mtp_max_connections,
        panel_hostname=cfg.panel_hostname,
    )


async def _read_text(ops: SystemOps, path: str) -> str | None:
    try:
        if not await ops.exists(path):
            return None
        return (await ops.read_file(path)).decode("utf-8", "replace")
    except SystemOpsError:
        return None


async def read_live_facts(ops: SystemOps, paths: ApplyPaths) -> MtproxyFacts:
    """Facts from the installed legacy mtproxy.service + drop-ins + EnvironmentFile contents."""
    unit_text: str | None = None
    unit_dir = paths.systemd_dir
    for base in (unit_dir, *(d for d in _UNIT_SEARCH if d != unit_dir)):
        unit_text = await _read_text(ops, f"{base}/{paths.legacy_unit}")
        if unit_text:
            break
    if not unit_text:
        raise RenderError("mtproxy.service not found or empty")
    dropins: list[str] = []
    dropin_dir = f"{unit_dir}/{paths.legacy_unit}.d"
    try:
        names = sorted(n for n in await ops.list_dir(dropin_dir) if n.endswith(".conf"))
    except SystemOpsError:
        names = []
    for name in names:
        text = await _read_text(ops, f"{dropin_dir}/{name}")
        if text:
            dropins.append(text)
    env_text = await _read_text(ops, paths.mtproxy_env) or ""
    env_files: list[str] = []
    seen: set[str] = {paths.mtproxy_env}
    for text in [unit_text, *dropins]:
        for match in _ENV_FILE_RE.finditer(text):
            env_path = match.group(1)
            if env_path in seen:
                continue
            seen.add(env_path)
            content = await _read_text(ops, env_path)
            if content is not None:
                env_files.append(content)
    return extract_mtproxy_facts(unit_text, dropins, env_text, env_files)


def facts_to_json(facts: MtproxyFacts) -> str:
    return json.dumps(asdict(facts), sort_keys=True)


def facts_from_json(raw: str) -> MtproxyFacts:
    data = json.loads(raw)
    return MtproxyFacts(**data)


async def resolve_facts(
    ops: SystemOps, paths: ApplyPaths, conn: sqlite3.Connection
) -> tuple[MtproxyFacts, list[str]]:
    """Live facts (cached in settings), falling back to the cache (e.g. legacy unit masked)."""
    warnings: list[str] = []
    try:
        facts = await read_live_facts(ops, paths)
    except (RenderError, SystemOpsError) as exc:
        cached = repo.get_setting(conn, KEY_MTPROXY_FACTS)
        if cached is None:
            raise RenderError(f"cannot determine MTProxy parameters: {exc}") from None
        warnings.append("Параметры MTProxy взяты из сохранённой копии (юнит недоступен)")
        return facts_from_json(cached), warnings
    encoded = facts_to_json(facts)
    if repo.get_setting(conn, KEY_MTPROXY_FACTS) != encoded:
        repo.set_setting(conn, KEY_MTPROXY_FACTS, encoded)
    if facts.nat_args_unresolved:
        warnings.append("NAT-аргументы MTProxy не найдены, хотя юнит на них ссылается")
    return facts, warnings


def pool_ids_of(pools: tuple[PoolRecord, ...]) -> set[int]:
    return {p.id for p in pools}
