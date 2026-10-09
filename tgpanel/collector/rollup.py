"""Nightly rollup of traffic tiers (minute -> hour -> day), PLAN 3.5.

``repo.rollup`` moves (adds into the destination tier and deletes from the source) buckets
older than a cutoff, so every byte lives in exactly one tier at any time: re-running is a
no-op and totals never change. Cutoffs come from ``domain.counters.rollup_cutoffs``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.collector import _config as cfg
from tgpanel.db import repo
from tgpanel.db.connection import transaction
from tgpanel.domain.counters import RetentionPolicy, RollupCutoffs, rollup_cutoffs

log = logging.getLogger("tgpanel.collector")

ROLLUP_HOUR_UTC = 3


@dataclass(frozen=True, slots=True)
class RollupResult:
    minute_rows: int  # minute rows folded into hours
    hour_rows: int  # hour rows folded into days


def retention_policy(settings: dict[str, str]) -> RetentionPolicy:
    minute_days = cfg.int_setting(
        settings, cfg.KEY_RETENTION_MINUTE, cfg.DEFAULT_RETENTION_MINUTE_DAYS, 1, 3650
    )
    hour_days = cfg.int_setting(
        settings, cfg.KEY_RETENTION_HOUR, cfg.DEFAULT_RETENTION_HOUR_DAYS, 1, 36500
    )
    return RetentionPolicy(minute_days=minute_days, hour_days=max(hour_days, minute_days))


def _do_rollup(conn: sqlite3.Connection, cutoffs: RollupCutoffs) -> RollupResult:
    with transaction(conn):
        # minute -> hour first: freshly created old hour rows are then picked up by hour -> day
        minute_rows = repo.rollup(conn, "minute", "hour", cutoffs.minute_before)
        hour_rows = repo.rollup(conn, "hour", "day", cutoffs.hour_before)
    return RollupResult(minute_rows, hour_rows)


def _do_rollup_marked(conn: sqlite3.Connection, cutoffs: RollupCutoffs, today: str) -> RollupResult:
    with transaction(conn):
        result = _do_rollup(conn, cutoffs)
        repo.set_setting(conn, cfg.KEY_ROLLUP_LAST, today)
    return result


async def rollup_and_prune(pipeline: ApplyPipeline, now: datetime) -> RollupResult:
    """Fold old buckets into coarser tiers per retention settings. Idempotent, one db_write."""
    settings = await pipeline.db.run(repo.all_settings)
    cutoffs = rollup_cutoffs(now.astimezone(UTC), retention_policy(settings))
    return await pipeline.db_write(_do_rollup, cutoffs)


async def maybe_rollup(pipeline: ApplyPipeline, now: datetime) -> bool:
    """Scheduler hook: run the rollup once per UTC day after 03:00. True if it ran."""
    now = now.astimezone(UTC)
    if now.hour < ROLLUP_HOUR_UTC:
        return False
    today = now.date().isoformat()
    settings = await pipeline.db.run(repo.all_settings)
    if settings.get(cfg.KEY_ROLLUP_LAST) == today:
        return False
    cutoffs = rollup_cutoffs(now, retention_policy(settings))
    result = await pipeline.db_write(_do_rollup_marked, cutoffs, today)
    log.info(
        "traffic rollup done: %s minute rows, %s hour rows", result.minute_rows, result.hour_rows
    )
    return True
