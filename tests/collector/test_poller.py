from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from tests.collector.conftest import SECRET_RE, T0, CEnv, at
from tgpanel.apply.errors import DbWriteTimeout
from tgpanel.db import repo
from tgpanel.domain.counters import Counter
from tgpanel.domain.queries import UserListQuery
from tgpanel.system.ops import SetCounter, SystemOpsError


async def test_first_observation_has_zero_delta(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    res = await cenv.collector.poll_once(T0)
    assert res.updated == 1
    assert cenv.traffic(ip) == []
    st = cenv.state(ip)
    assert st is not None and st.up.bytes == 5000 and not st.active_last
    assert cenv.user_extra(ip).first_seen_at is None


async def test_delta_written_to_minute_bucket(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=1000, up_packets=4, down_bytes=500, down_packets=2)
    await cenv.collector.poll_once(at(30))
    cenv.fake.add_traffic(ip, up_bytes=100, up_packets=1)
    await cenv.collector.poll_once(at(45))  # same minute: additive upsert
    (p,) = cenv.traffic(ip)
    assert p.ts == T0
    assert (p.bytes_up, p.bytes_down, p.packets_up, p.packets_down) == (1100, 500, 5, 2)


async def test_new_element_after_first_poll_counts_from_zero(cenv: CEnv) -> None:
    await cenv.collector.poll_once(T0)  # collector is running, nobody exists
    ip = cenv.add_user(2, created_at=at(10))  # apply added both set elements
    cenv.fake.add_traffic(ip, up_bytes=700, up_packets=5)
    await cenv.collector.poll_once(at(30))
    (p,) = cenv.traffic(ip)
    assert p.bytes_up == 700 and p.packets_up == 5


async def test_old_user_without_state_is_not_counted_in_full(cenv: CEnv) -> None:
    await cenv.collector.poll_once(T0)
    ip = cenv.add_user(2, created_at=T0 - timedelta(days=30))
    cenv.fake.add_traffic(ip, up_bytes=10**9, up_packets=10**6, create=True)
    await cenv.collector.poll_once(at(30))
    assert cenv.traffic(ip) == []


async def test_counter_reset_no_negative_no_spike(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(30))
    before = cenv.traffic(ip)
    # table reloaded: counters restart from small values
    cenv.fake.nft_sets[("tgpanel", "up")][ip] = SetCounter(100, 2)
    cenv.fake.nft_sets[("tgpanel", "down")][ip] = SetCounter(50, 1)
    await cenv.collector.poll_once(at(60))
    assert cenv.traffic(ip) == before
    st = cenv.state(ip)
    assert st is not None and st.up == Counter(100, 2) and not st.active_last
    # growth after the reset is counted from the new base
    cenv.fake.add_traffic(ip, up_bytes=40, up_packets=1)
    await cenv.collector.poll_once(at(90))
    assert sum(p.bytes_up for p in cenv.traffic(ip)) == 5000 + 40


async def test_missing_element_keeps_state(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=900, up_packets=3)
    await cenv.collector.poll_once(at(30))
    del cenv.fake.nft_sets[("tgpanel", "up")][ip]
    del cenv.fake.nft_sets[("tgpanel", "down")][ip]
    res = await cenv.collector.poll_once(at(60))
    assert res.updated == 0
    st = cenv.state(ip)
    assert st is not None and st.up.bytes == 900 and st.updated_at == at(30)


@pytest.mark.parametrize(
    ("nbytes", "npackets", "active"),
    [(2048, 11, False), (2049, 10, False), (2049, 11, True), (0, 0, False)],
)
async def test_activity_threshold_boundaries(
    cenv: CEnv, nbytes: int, npackets: int, active: bool
) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=nbytes, up_packets=npackets)
    await cenv.collector.poll_once(at(30))
    st = cenv.state(ip)
    assert st is not None and st.active_last is active
    assert (cenv.user_extra(ip).last_seen_at is not None) is active


async def test_activity_threshold_from_settings(cenv: CEnv) -> None:
    cenv.db.call(repo.set_setting, "activity_min_bytes", "10")
    cenv.db.call(repo.set_setting, "activity_min_packets", "1")
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=11, up_packets=2)
    await cenv.collector.poll_once(at(30))
    st = cenv.state(ip)
    assert st is not None and st.active_last


async def test_first_seen_set_once_last_seen_updates(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(30))
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(60))
    extra = cenv.user_extra(ip)
    assert extra.first_seen_at == at(30)
    assert extra.last_seen_at == at(60)


async def test_online_after_two_active_polls_offline_when_quiet(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)

    def online(now_s: int) -> bool:
        rows, _ = cenv.db.call(repo.list_users, UserListQuery(), at(now_s))
        return rows[0].online

    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(30))
    assert not online(30)  # one active poll only
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(60))
    assert online(60)
    await cenv.collector.poll_once(at(90))  # quiet
    assert not online(90)
    # a dead collector means nobody is online
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(120))
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    await cenv.collector.poll_once(at(150))
    assert online(150) and not online(150 + 200)


async def test_missing_table_is_quiet_skip(cenv: CEnv, caplog: pytest.LogCaptureFixture) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=5000, up_packets=20, down_bytes=3000, down_packets=15)
    del cenv.fake.nft_sets[("tgpanel", "up")], cenv.fake.nft_sets[("tgpanel", "down")]
    with caplog.at_level(logging.WARNING, logger="tgpanel.collector"):
        res = await cenv.collector.poll_once(at(30))
    assert res.skipped == "nft"
    st = cenv.state(ip)
    assert st is not None and st.updated_at == T0
    assert any("skipped" in r.message for r in caplog.records)


async def test_nft_error_does_not_crash_and_recovers(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.fail_on("nft_list_set", exc=SystemOpsError, times=1)
    assert (await cenv.collector.poll_once(at(30))).skipped == "nft"
    cenv.fake.fail_on("nft_list_set", exc=RuntimeError, times=1)
    assert (await cenv.collector.poll_once(at(45))).skipped == "error"
    cenv.fake.add_traffic(ip, up_bytes=100, up_packets=2)
    await cenv.collector.poll_once(at(60))
    assert sum(p.bytes_up for p in cenv.traffic(ip)) == 100


async def test_db_busy_drops_sample_and_next_poll_catches_up(
    cenv: CEnv, caplog: pytest.LogCaptureFixture
) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=500, up_packets=3)
    await cenv.pipeline._db_lock.acquire()  # an apply holds the write lock
    try:
        with caplog.at_level(logging.WARNING, logger="tgpanel.collector"):
            res = await cenv.collector.poll_once(at(30))
    finally:
        cenv.pipeline._db_lock.release()
    assert res.dropped
    assert cenv.traffic(ip) == []
    assert any("dropped" in r.message for r in caplog.records)
    await cenv.collector.poll_once(at(60))  # state was not advanced: nothing lost
    assert sum(p.bytes_up for p in cenv.traffic(ip)) == 500


async def test_db_write_timeout_exception_is_handled(cenv: CEnv) -> None:
    ip = cenv.add_user(1)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip, up_bytes=5, up_packets=1)

    async def boom(*_a: object, **_k: object) -> None:
        raise DbWriteTimeout("busy")

    cenv.pipeline.db_write = boom  # type: ignore[method-assign,assignment]
    assert (await cenv.collector.poll_once(at(30))).dropped


async def test_all_writes_in_one_db_write_per_poll(cenv: CEnv) -> None:
    for n in (1, 2, 3):
        cenv.add_user(n)
    calls: list[int] = []
    original = cenv.pipeline.db_write

    async def counting(fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return await original(fn, *args, **kwargs)

    cenv.pipeline.db_write = counting  # type: ignore[method-assign]
    await cenv.collector.poll_once(T0)
    assert len(calls) == 1


async def test_deleted_user_between_read_and_write_does_not_fail(cenv: CEnv) -> None:
    ip1 = cenv.add_user(1)
    cenv.add_user(2)
    await cenv.collector.poll_once(T0)
    cenv.fake.add_traffic(ip1, up_bytes=50, up_packets=1)
    original = cenv.fake.nft_list_set
    victim = cenv.uid(ip1)

    async def deleting(table: str, name: str) -> dict[str, SetCounter]:
        result = await original(table, name)
        cenv.db.call(repo.delete_users, [victim])
        return result

    cenv.fake.nft_list_set = deleting  # type: ignore[method-assign,assignment]
    res = await cenv.collector.poll_once(at(30))
    assert res.skipped is None


async def test_run_loop_polls_until_stopped(cenv: CEnv) -> None:
    cenv.db.call(repo.set_setting, "poll_interval_s", "5")
    stop = asyncio.Event()
    task = asyncio.create_task(cenv.collector.run(stop))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if cenv.fake.call_count("nft_list_set") >= 2:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert cenv.fake.call_count("nft_list_set") >= 2


async def test_no_secrets_in_logs(cenv: CEnv, caplog: pytest.LogCaptureFixture) -> None:
    cenv.add_user(1)
    cenv.fake.fail_on("nft_list_set", exc=SystemOpsError("bad " + "ab" * 16), times=1)
    with caplog.at_level(logging.DEBUG):
        await cenv.collector.poll_once(T0)
        cenv.fake.fail_on("nft_list_set", exc=RuntimeError("dd" + "cd" * 16), times=1)
        await cenv.collector.poll_once(at(30))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text
    assert not SECRET_RE.search(text)
    for s in cenv.secrets.values():
        assert s not in text
