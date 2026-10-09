from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.collector.conftest import CEnv
from tgpanel.collector.rollup import maybe_rollup, rollup_and_prune
from tgpanel.db import repo

NOW = datetime(2026, 6, 20, 4, 30, 0, tzinfo=UTC)


def _seed(env: CEnv, uid: int) -> None:
    """Minute rows for 40 days back, hour rows for 300 days back, day rows older."""

    def go(conn):  # type: ignore[no-untyped-def]
        for i in range(0, 40 * 24 * 60, 37):
            ts = (NOW - timedelta(minutes=i)).replace(second=0)
            repo.add_traffic(
                conn, "minute", uid, ts, bytes_up=i % 91 + 1, bytes_down=3, packets_up=1
            )
        for i in range(0, 300 * 24, 5):
            ts = (NOW - timedelta(hours=i)).replace(minute=0, second=0)
            repo.add_traffic(conn, "hour", uid, ts, bytes_up=7, bytes_down=i % 13 + 1)
        for i in range(0, 400):
            ts = (NOW - timedelta(days=i)).replace(hour=0, minute=0, second=0)
            repo.add_traffic(conn, "day", uid, ts, bytes_up=11, bytes_down=2)

    env.db.call(go)


def _sums(env: CEnv, uid: int) -> tuple[int, int]:
    return env.db.call(repo.traffic_totals, uid)


def _counts(env: CEnv) -> dict[str, int]:
    return {
        t: env.db.call(lambda c, t=t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608
        for t in ("traffic_minute", "traffic_hour", "traffic_day")
    }


async def test_rollup_conserves_totals_and_is_idempotent(cenv: CEnv) -> None:
    cenv.add_user(1)
    uid = cenv.uid("127.64.0.1")
    _seed(cenv, uid)
    before = _sums(cenv, uid)
    first = await rollup_and_prune(cenv.pipeline, NOW)
    assert first.minute_rows > 0 and first.hour_rows > 0
    assert _sums(cenv, uid) == before
    snapshot = _counts(cenv)
    second = await rollup_and_prune(cenv.pipeline, NOW)
    assert (second.minute_rows, second.hour_rows) == (0, 0)
    assert _sums(cenv, uid) == before
    assert _counts(cenv) == snapshot


async def test_rollup_respects_retention_tiers(cenv: CEnv) -> None:
    cenv.add_user(1)
    uid = cenv.uid("127.64.0.1")
    _seed(cenv, uid)
    await rollup_and_prune(cenv.pipeline, NOW)
    min_cut = (NOW - timedelta(days=14)).replace(minute=0, second=0)
    hour_cut = (NOW - timedelta(days=180)).replace(hour=0, minute=0, second=0)
    far = datetime(2000, 1, 1, tzinfo=UTC)
    assert not cenv.db.call(repo.get_traffic, "minute", uid, far, min_cut)
    assert cenv.db.call(repo.get_traffic, "minute", uid, min_cut, NOW)
    assert not cenv.db.call(repo.get_traffic, "hour", uid, far, hour_cut)
    assert cenv.db.call(repo.get_traffic, "hour", uid, hour_cut, min_cut)
    # rolled-up hour buckets are whole hours; day buckets whole days
    assert all(p.ts.minute == 0 for p in cenv.db.call(repo.get_traffic, "hour", uid, far, NOW))
    assert all(p.ts.hour == 0 for p in cenv.db.call(repo.get_traffic, "day", uid, far, NOW))


async def test_rollup_uses_retention_settings(cenv: CEnv) -> None:
    cenv.add_user(1)
    uid = cenv.uid("127.64.0.1")
    _seed(cenv, uid)
    cenv.db.call(repo.set_setting, "retention_minute_days", "3")
    cenv.db.call(repo.set_setting, "retention_hour_days", "30")
    before = _sums(cenv, uid)
    await rollup_and_prune(cenv.pipeline, NOW)
    far = datetime(2000, 1, 1, tzinfo=UTC)
    cut = (NOW - timedelta(days=3)).replace(minute=0, second=0)
    assert not cenv.db.call(repo.get_traffic, "minute", uid, far, cut)
    assert _sums(cenv, uid) == before


async def test_rollup_garbage_settings_fall_back_to_defaults(cenv: CEnv) -> None:
    cenv.add_user(1)
    cenv.db.call(repo.set_setting, "retention_minute_days", "abc")
    cenv.db.call(repo.set_setting, "retention_hour_days", "-5")
    await rollup_and_prune(cenv.pipeline, NOW)  # must not raise


async def test_rollup_is_one_db_write(cenv: CEnv) -> None:
    calls: list[int] = []
    original = cenv.pipeline.db_write

    async def counting(fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return await original(fn, *args, **kwargs)

    cenv.pipeline.db_write = counting  # type: ignore[method-assign]
    await rollup_and_prune(cenv.pipeline, NOW)
    assert len(calls) == 1


async def test_maybe_rollup_once_per_utc_day_after_three(cenv: CEnv) -> None:
    cenv.add_user(1)
    uid = cenv.uid("127.64.0.1")
    _seed(cenv, uid)
    early = NOW.replace(hour=2, minute=59)
    assert await maybe_rollup(cenv.pipeline, early) is False
    assert await maybe_rollup(cenv.pipeline, NOW.replace(hour=3, minute=0)) is True
    assert await maybe_rollup(cenv.pipeline, NOW.replace(hour=23)) is False
    assert cenv.db.call(repo.get_setting, "collector.rollup_last_date") == "2026-06-20"
    assert await maybe_rollup(cenv.pipeline, NOW + timedelta(days=1)) is True
