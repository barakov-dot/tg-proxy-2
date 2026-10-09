"""Single entry point for apply/: render every proxy-side file from the desired state."""

from __future__ import annotations

from dataclasses import dataclass

from tgpanel.domain.models import DesiredState
from tgpanel.render.config import patch_config
from tgpanel.render.errors import RenderError
from tgpanel.render.nft import render_nft
from tgpanel.render.pools import MtproxyFacts, pool_secrets, render_pool_env, render_pool_unit
from tgpanel.render.profiles import parse_profiles, render_profiles


@dataclass(frozen=True, slots=True)
class RenderedFiles:
    profiles_json: bytes
    config_json: bytes
    pool_envs: dict[int, bytes]  # only pools that must run
    pool_unit: bytes
    nft_file: bytes
    pools_to_run: tuple[int, ...]
    pools_to_stop: tuple[int, ...]


def compute_relay_limits(state: DesiredState, profile_count: int) -> dict[str, int]:
    """Relay limits written into config.json (PLAN 3.7)."""
    sessions = state.relay_limits.max_sessions_global
    return {
        "max_profiles": max(32, profile_count + 16),
        "max_sessions_global": sessions,
        "new_sessions_burst": sessions,
        "max_bootstraps_global": sessions,
        "new_bootstraps_burst": sessions,
        "max_streams_global": state.relay_limits.max_streams_global,
    }


def render_all(state: DesiredState, existing_config: bytes, facts: MtproxyFacts) -> RenderedFiles:
    if not 1 <= state.secrets_per_process <= 16:
        raise RenderError("secrets_per_process must be within 1..16")
    ids = [p.id for p in state.pools]
    if len(set(ids)) != len(ids):
        raise RenderError("duplicate pool ids")

    profiles = render_profiles(state)
    profile_count = len(parse_profiles(profiles))

    envs: dict[int, bytes] = {}
    stop: list[int] = []
    for pool in sorted((p for p in state.pools if p.managed), key=lambda p: p.id):
        secrets = pool_secrets(state, pool)
        if not secrets:
            stop.append(pool.id)
            continue
        if len(secrets) > state.secrets_per_process:
            raise RenderError(
                f"pool {pool.id} has {len(secrets)} secrets, limit is {state.secrets_per_process}"
            )
        envs[pool.id] = render_pool_env(
            pool, secrets, state.mtp_workers, state.mtp_max_connections, facts.nat_args
        )

    return RenderedFiles(
        profiles_json=profiles,
        config_json=patch_config(existing_config, compute_relay_limits(state, profile_count)),
        pool_envs=envs,
        pool_unit=render_pool_unit(facts),
        nft_file=render_nft(state),
        pools_to_run=tuple(envs),
        pools_to_stop=tuple(stop),
    )
