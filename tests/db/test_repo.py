import sqlite3
from datetime import timedelta

import pytest

from tests.db.helpers import NOW, add_user, fresh_db
from tgpanel.db import repo
from tgpanel.domain.counters import Counter
from tgpanel.domain.models import CarrierMode, PoolRecord, UserStatus


def test_pools_crud() -> None:
    conn = fresh_db()
    assert repo.list_pools(conn) == [
        PoolRecord(1, 2400, 8900, True),
        PoolRecord(2, 2401, 8901, True),
    ]
    repo.insert_pool(conn, PoolRecord(3, 2402, 8902, managed=False), NOW)
    assert repo.get_pool(conn, 3) == PoolRecord(3, 2402, 8902, False)
    repo.delete_pool(conn, 3)
    assert repo.get_pool(conn, 3) is None


def test_user_roundtrip_to_record_and_extra() -> None:
    conn = fresh_db()
    uid = add_user(
        conn,
        1,
        status=UserStatus.DISABLED,
        carrier_mode=CarrierMode.HTTPS_LANES,
        expires_at=NOW + timedelta(days=3),
        comment="import",
        tg_id=555,
        tg_username="alice",
        imported=True,
        source_profile_name="user_555",
        bot_started=True,
    )
    u = repo.get_user(conn, uid)
    assert u is not None
    assert u.name == "user1" and u.status is UserStatus.DISABLED
    assert u.carrier_mode is CarrierMode.HTTPS_LANES
    assert u.expires_at == NOW + timedelta(days=3)
    assert (u.tg_id, u.imported, u.source_profile_name) == (555, True, "user_555")
    assert u.profile_name == f"u{uid}"
    x = repo.get_user_extra(conn, uid)
    assert x is not None and x.tg_username == "alice" and x.bot_started and x.created_at == NOW
    assert repo.get_user(conn, 999) is None
    assert repo.get_user_by_tg_id(conn, 555) == u
    assert repo.get_user_by_secret(conn, u.secret) == u


def test_update_user_whitelist_and_conversions() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    repo.update_user(
        conn,
        uid,
        status=UserStatus.EXPIRED,
        expires_at=NOW,
        carrier_mode=CarrierMode.WEBSOCKET,
        bot_started=True,
        comment="x",
    )
    u = repo.get_user(conn, uid)
    assert u is not None
    assert (u.status, u.expires_at, u.carrier_mode, u.comment) == (
        UserStatus.EXPIRED, NOW, CarrierMode.WEBSOCKET, "x",
    )  # fmt: skip
    repo.update_user(conn, uid, expires_at=None, carrier_mode=None)
    u = repo.get_user(conn, uid)
    assert u is not None and u.expires_at is None and u.carrier_mode is None
    with pytest.raises(ValueError):
        repo.update_user(conn, uid, id=5)
    with pytest.raises(ValueError):
        repo.update_user(conn, uid, **{"comment = 'x'; --": 1})


def test_naive_datetimes_rejected() -> None:
    from datetime import datetime

    conn = fresh_db()
    with pytest.raises(ValueError):
        add_user(conn, 1, created_at=datetime(2026, 1, 1))


def test_delete_users_cascades() -> None:
    conn = fresh_db()
    a, b = add_user(conn, 1), add_user(conn, 2)
    repo.add_traffic(conn, "minute", a, NOW, bytes_up=5)
    repo.put_counter_state(
        conn, repo.CounterStateRow(a, Counter(1, 1), Counter(2, 2), True, False, NOW)
    )
    repo.delete_users(conn, [a])
    assert [u.id for u in repo.all_users(conn)] == [b]
    assert conn.execute("SELECT COUNT(*) FROM traffic_minute").fetchone()[0] == 0
    assert repo.get_counter_state(conn, a) is None


def test_users_by_ids_ips_and_occupancy() -> None:
    conn = fresh_db()
    ids = [add_user(conn, i, pool_id=1 if i < 3 else 2) for i in range(1, 5)]
    assert [u.id for u in repo.users_by_ids(conn, ids[1:3])] == ids[1:3]
    assert repo.users_by_ids(conn, []) == []
    assert sorted(repo.used_loopback_ips(conn)) == [f"127.64.0.{i}" for i in range(1, 5)]
    assert repo.pool_occupancy(conn) == {1: 2, 2: 2}


def test_settings_admins() -> None:
    conn = fresh_db()
    assert repo.get_setting(conn, "k") is None
    assert repo.get_setting(conn, "k", "d") == "d"
    repo.set_setting(conn, "k", "1")
    repo.set_setting(conn, "k", "2")
    assert repo.get_setting(conn, "k") == "2" and repo.all_settings(conn) == {"k": "2"}
    repo.delete_setting(conn, "k")
    assert repo.all_settings(conn) == {}
    repo.add_admin(conn, 5, NOW)
    repo.add_admin(conn, 5, NOW)
    repo.add_admin(conn, 3, NOW)
    assert repo.list_admins(conn) == [3, 5] and repo.is_admin(conn, 5)
    repo.remove_admin(conn, 5)
    assert not repo.is_admin(conn, 5)


def test_audit_log() -> None:
    conn = fresh_db()
    repo.add_audit(conn, NOW, "web:admin", "user.create", "user:1", "ok")
    repo.add_audit(conn, NOW + timedelta(seconds=1), "system", "expire", "user:2")
    entries = repo.list_audit(conn)
    assert [e.action for e in entries] == ["expire", "user.create"]
    assert [e.action for e in repo.list_audit(conn, target="user:1")] == ["user.create"]
    assert len(repo.list_audit(conn, limit=1, offset=1)) == 1


def test_apply_runs() -> None:
    conn = fresh_db()
    rid = repo.start_apply_run(conn, NOW, "create user", "/b/x.tgz")
    run = repo.get_apply_run(conn, rid)
    assert run is not None and run.status == "running" and run.finished_at is None
    repo.finish_apply_run(conn, rid, NOW + timedelta(seconds=3), "failed", error="rolled back")
    run = repo.get_apply_run(conn, rid)
    assert run is not None
    assert (run.status, run.error, run.backup_path) == ("failed", "rolled back", "/b/x.tgz")
    rid2 = repo.start_apply_run(conn, NOW, "second")
    assert [r.id for r in repo.list_apply_runs(conn)] == [rid2, rid]


def test_backups() -> None:
    conn = fresh_db()
    a = repo.add_backup(conn, "/b/a.tgz", NOW, "pre-install", 10)
    b = repo.add_backup(conn, "/b/b.tgz", NOW + timedelta(days=1), "apply", 20)
    assert [x.id for x in repo.list_backups(conn)] == [b, a]
    got = repo.get_backup(conn, a)
    assert got is not None and got.size == 10 and got.reason == "pre-install"
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_backup(conn, "/b/a.tgz", NOW, "dup", 1)
    repo.delete_backup(conn, a)
    assert repo.get_backup(conn, a) is None


def test_access_requests() -> None:
    conn = fresh_db()
    rid = repo.create_access_request(conn, 77, "bob", "Bob B", NOW)
    with pytest.raises(sqlite3.IntegrityError):  # one pending per tg_id
        repo.create_access_request(conn, 77, "bob", "Bob B", NOW)
    pending = repo.pending_request_for(conn, 77)
    assert pending is not None and pending.id == rid and pending.status == "pending"
    repo.decide_access_request(conn, rid, "approved", "web:admin", NOW)
    req = repo.get_access_request(conn, rid)
    assert req is not None and req.status == "approved" and req.decided_by == "web:admin"
    assert repo.pending_request_for(conn, 77) is None
    repo.create_access_request(conn, 77, None, "Bob", NOW)  # allowed again
    assert len(repo.list_access_requests(conn)) == 2
    assert len(repo.list_access_requests(conn, "pending")) == 1
    with pytest.raises(ValueError):
        repo.decide_access_request(conn, rid, "pending", "x", NOW)


def test_broadcasts() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    bid = repo.create_broadcast(conn, "web:admin", NOW, "hello {link}")
    item = repo.add_broadcast_item(conn, bid, uid, 5)
    repo.set_broadcast_item_result(conn, item, "sent", NOW)
    rows = repo.list_broadcast_items(conn, bid)
    assert [r["result"] for r in rows] == ["sent"]


def test_traffic_upsert_get_totals_and_tier_whitelist() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    repo.add_traffic(conn, "minute", uid, NOW, bytes_up=10, bytes_down=20, packets_up=1)
    repo.add_traffic(conn, "minute", uid, NOW, bytes_up=5, packets_down=2)
    pts = repo.get_traffic(conn, "minute", uid, NOW, NOW + timedelta(minutes=1))
    assert pts == [repo.TrafficPoint(NOW, 15, 20, 1, 2)]
    assert (
        repo.get_traffic(conn, "minute", uid, NOW + timedelta(minutes=1), NOW + timedelta(hours=1))
        == []
    )
    repo.add_traffic(conn, "day", uid, NOW.replace(hour=0), bytes_up=100)
    assert repo.traffic_totals(conn, uid) == (115, 20)
    assert repo.traffic_totals(conn, uid, NOW) == (15, 20)
    for bad in ("minute; DROP TABLE users", "users", ""):
        with pytest.raises(ValueError):
            repo.add_traffic(conn, bad, uid, NOW)
        with pytest.raises(ValueError):
            repo.get_traffic(conn, bad, uid, NOW, NOW)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_rollup_minute_hour_day_conserves_totals() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    base = NOW.replace(minute=0)
    for m in range(0, 120, 7):
        repo.add_traffic(
            conn, "minute", uid, base + timedelta(minutes=m), bytes_up=3, bytes_down=4, packets_up=1
        )
    before = repo.traffic_totals(conn, uid)
    moved = repo.rollup(conn, "minute", "hour", base + timedelta(hours=1))
    assert moved == 9  # minutes 0..56 step 7
    assert repo.traffic_totals(conn, uid) == before
    hours = repo.get_traffic(conn, "hour", uid, base - timedelta(days=1), base + timedelta(days=1))
    assert [h.ts for h in hours] == [base] and hours[0].bytes_up == 27 and hours[0].packets_up == 9
    # a second rollup into the same hour bucket is additive
    repo.add_traffic(conn, "minute", uid, base + timedelta(minutes=59), bytes_up=1)
    repo.rollup(conn, "minute", "hour", base + timedelta(hours=1))
    assert repo.traffic_totals(conn, uid)[0] == before[0] + 1
    repo.rollup(conn, "hour", "day", base + timedelta(days=1))
    days = repo.get_traffic(conn, "day", uid, base - timedelta(days=2), base + timedelta(days=2))
    assert len(days) == 1 and days[0].ts == base.replace(hour=0)
    assert repo.traffic_totals(conn, uid)[0] == before[0] + 1
    with pytest.raises(ValueError):
        repo.rollup(conn, "day", "hour", NOW)


def test_delete_traffic_before() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    repo.add_traffic(conn, "minute", uid, NOW - timedelta(days=20), bytes_up=1)
    repo.add_traffic(conn, "minute", uid, NOW, bytes_up=1)
    assert repo.delete_traffic_before(conn, "minute", NOW - timedelta(days=14)) == 1
    assert repo.traffic_totals(conn, uid) == (1, 0)


def test_counter_state_roundtrip_and_upsert() -> None:
    conn = fresh_db()
    uid = add_user(conn, 1)
    row = repo.CounterStateRow(uid, Counter(10, 2), Counter(20, 3), True, False, NOW)
    repo.put_counter_state(conn, row)
    assert repo.get_counter_state(conn, uid) == row
    row2 = repo.CounterStateRow(uid, Counter(11, 3), Counter(21, 4), True, True, NOW)
    repo.put_counter_state(conn, row2)
    assert repo.get_counter_state(conn, uid) == row2
    repo.delete_counter_state(conn, uid)
    assert repo.get_counter_state(conn, uid) is None
