"""Traffic collector: nft counters -> per-user minute buckets (PLAN 3.4, 3.5).

Reads only ``nft list set`` (never /readyz). All writes of one poll go through ONE
``pipeline.db_write`` call; if the database is busy with an apply the sample is dropped
(counter state is not advanced, so the next poll simply covers a longer interval).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tgpanel.apply.errors import DbWriteTimeout
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.collector import _config as cfg
from tgpanel.db import repo
from tgpanel.db.connection import transaction
from tgpanel.domain.counters import (
    ACTIVITY_MIN_BYTES,
    ACTIVITY_MIN_PACKETS,
    Counter,
    compute_delta,
    floor_minute,
    is_active,
)
from tgpanel.domain.models import UserRecord
from tgpanel.system.ops import SetCounter, SystemOps, SystemOpsError

log = logging.getLogger("tgpanel.collector")

MAX_WRITE_WAIT_S = 10.0


@dataclass(frozen=True, slots=True)
class _Update:
    user_id: int
    state: repo.CounterStateRow
    bytes_up: int
    bytes_down: int
    packets_up: int
    packets_down: int
    active: bool


@dataclass(frozen=True, slots=True)
class PollResult:
    updated: int = 0  # users whose state was written
    skipped: str | None = None  # reason when nothing was read/written
    dropped: bool = False  # sample computed but the write was dropped (database busy)


def _clock_utc() -> datetime:
    return datetime.now(UTC)


class Collector:
    def __init__(
        self,
        ops: SystemOps,
        pipeline: ApplyPipeline,
        *,
        clock: Callable[[], datetime] = _clock_utc,
        write_timeout_s: float | None = None,
    ) -> None:
        self._ops = ops
        self._pipeline = pipeline
        self._db = pipeline.db
        self._clock = clock
        self._write_timeout_s = write_timeout_s
        self._first_poll_at: datetime | None = None
        self._interval_s = cfg.DEFAULT_POLL_INTERVAL_S

    # ------------------------------------------------------------------ one poll

    async def poll_once(self, now: datetime | None = None) -> PollResult:
        now = self._clock() if now is None else now
        try:
            return await self._poll(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the loop must never die
            cfg.log_warning(log, "collector poll failed", exc)
            return PollResult(skipped="error")

    async def _poll(self, now: datetime) -> PollResult:
        settings, users, states, created = await self._db.run(_load)
        self._interval_s = cfg.int_setting(
            settings, cfg.KEY_POLL_INTERVAL, cfg.DEFAULT_POLL_INTERVAL_S, 5, 3600
        )
        try:
            up = await self._ops.nft_list_set(cfg.NFT_TABLE, "up")
            down = await self._ops.nft_list_set(cfg.NFT_TABLE, "down")
        except SystemOpsError as exc:
            cfg.log_warning(log, "nft counters unavailable, poll skipped", exc)
            return PollResult(skipped="nft")

        min_bytes = cfg.int_setting(settings, cfg.KEY_ACTIVITY_BYTES, ACTIVITY_MIN_BYTES, 0, 10**9)
        min_packets = cfg.int_setting(
            settings, cfg.KEY_ACTIVITY_PACKETS, ACTIVITY_MIN_PACKETS, 0, 10**9
        )
        first = self._first_poll_at is None
        first_at = now if self._first_poll_at is None else self._first_poll_at
        self._first_poll_at = first_at
        # A user created after the collector started (or just before) and not seen yet has a
        # fresh set element: its first reading counts in full (base 0).
        new_since = first_at - timedelta(seconds=2 * self._interval_s)

        updates: list[_Update] = []
        for user in users:
            update = _compute(
                user,
                up.get(user.loopback_ip),
                down.get(user.loopback_ip),
                states.get(user.id),
                now=now,
                new_element=not first and user.id in created and created[user.id] >= new_since,
                min_bytes=min_bytes,
                min_packets=min_packets,
            )
            if update is not None:
                updates.append(update)
        if not updates:
            return PollResult(skipped=None)

        wait_s = (
            self._write_timeout_s
            if self._write_timeout_s is not None
            else min(float(self._interval_s), MAX_WRITE_WAIT_S)
        )
        try:
            await self._pipeline.db_write(_write, updates, now, wait_s=wait_s)
        except DbWriteTimeout:
            log.warning("collector: database busy (apply in progress), sample dropped")
            return PollResult(dropped=True)
        return PollResult(updated=len(updates))

    # ------------------------------------------------------------------ loop

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), self._interval_s)


def _load(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], list[UserRecord], dict[int, repo.CounterStateRow], dict[int, datetime]]:
    """Settings, users, counter states and created_at of users that have no state yet."""
    users = repo.all_users(conn)
    states: dict[int, repo.CounterStateRow] = {}
    created: dict[int, datetime] = {}
    for u in users:
        st = repo.get_counter_state(conn, u.id)
        if st is not None:
            states[u.id] = st
        else:
            extra = repo.get_user_extra(conn, u.id)
            if extra is not None:
                created[u.id] = extra.created_at
    return repo.all_settings(conn), users, states, created


def _compute(
    user: UserRecord,
    up: SetCounter | None,
    down: SetCounter | None,
    prev: repo.CounterStateRow | None,
    *,
    now: datetime,
    new_element: bool,
    min_bytes: int,
    min_packets: int,
) -> _Update | None:
    """Pure per-user step. None = nothing to record (element missing)."""
    if up is None and down is None:
        return None  # element absent (disabled user / reload): keep state untouched
    if prev is None and (up is None or down is None):
        return None  # half-present element without a base: wait for the full pair
    d_up = _direction(prev.up if prev else None, up, new_element)
    d_down = _direction(prev.down if prev else None, down, new_element)
    total_b = d_up[0] + d_down[0]
    total_p = d_up[1] + d_down[1]
    reset = d_up[3] or d_down[3]
    active = (not reset) and is_active(
        total_b, total_p, min_bytes=min_bytes, min_packets=min_packets
    )
    state = repo.CounterStateRow(
        user_id=user.id,
        up=d_up[2],
        down=d_down[2],
        active_last=active,
        active_prev=prev.active_last if prev else False,
        updated_at=now,
    )
    return _Update(user.id, state, d_up[0], d_down[0], d_up[1], d_down[1], active)


def _direction(
    prev: Counter | None, cur: SetCounter | None, new_element: bool
) -> tuple[int, int, Counter, bool]:
    """(bytes, packets, new stored state, reset) for one direction."""
    if cur is None:
        # the caller guarantees prev is set here (half-present elements without a base are skipped)
        return 0, 0, prev if prev is not None else Counter(0, 0), False
    delta = compute_delta(prev, Counter(cur.bytes, cur.packets), new_element=new_element)
    return delta.bytes, delta.packets, delta.state, delta.reset


def _write(conn: sqlite3.Connection, updates: list[_Update], now: datetime) -> None:
    bucket = floor_minute(now)
    with transaction(conn):
        existing = {int(r[0]) for r in conn.execute("SELECT id FROM users")}
        for u in updates:
            if u.user_id not in existing:
                continue  # deleted while we were polling
            repo.put_counter_state(conn, u.state)
            if u.bytes_up or u.bytes_down or u.packets_up or u.packets_down:
                repo.add_traffic(
                    conn,
                    "minute",
                    u.user_id,
                    bucket,
                    bytes_up=u.bytes_up,
                    bytes_down=u.bytes_down,
                    packets_up=u.packets_up,
                    packets_down=u.packets_down,
                )
            if u.active:
                extra = repo.get_user_extra(conn, u.user_id)
                fields: dict[str, datetime] = {"last_seen_at": now}
                if extra is not None and extra.first_seen_at is None:
                    fields["first_seen_at"] = now
                repo.update_user(conn, u.user_id, **fields)
