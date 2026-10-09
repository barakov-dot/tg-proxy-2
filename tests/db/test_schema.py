import sqlite3
import stat
from pathlib import Path

import pytest

from tests.db.helpers import NOW, add_user, fresh_db
from tgpanel.db.connection import (
    Database,
    connect,
    current_version,
    migrate,
    open_database,
    snapshot_to,
    transaction,
)
from tgpanel.db.migrations import MIGRATIONS
from tgpanel.domain.models import CarrierMode, PoolRecord, UserStatus

TABLES = {
    "users", "pools", "traffic_minute", "traffic_hour", "traffic_day", "counter_state",
    "access_requests", "settings", "admins", "apply_runs", "backups", "broadcasts",
    "broadcast_items", "audit_log", "schema_version",
}  # fmt: skip


def test_schema_has_all_tables_and_user_columns() -> None:
    conn = open_database()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    assert cols == {
        "id", "name", "comment", "tg_id", "tg_username", "secret", "status", "disabled_reason",
        "pool_id", "loopback_ip", "carrier_mode", "created_at", "expires_at", "first_seen_at",
        "last_seen_at", "can_message", "bot_started", "imported", "source_profile_name",
    }  # fmt: skip


def test_pragmas_wal_and_fk(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(conn.execute("SELECT 1 AS a").fetchone(), sqlite3.Row)


def test_migrate_is_idempotent() -> None:
    conn = connect()
    assert current_version(conn) == 0
    assert migrate(conn) == MIGRATIONS[-1][0]
    assert migrate(conn) == MIGRATIONS[-1][0]
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(MIGRATIONS)


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from tgpanel.db import connection

    bad = ((1, "CREATE TABLE a (x INTEGER); CREATE TABLE a (y INTEGER);"),)
    monkeypatch.setattr(connection, "MIGRATIONS", bad)
    conn = connect()
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn)
    assert not conn.in_transaction
    assert current_version(conn) == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None


def test_unique_constraints() -> None:
    conn = fresh_db()
    add_user(conn, 1, tg_id=100)
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 2, tg_id=100, loopback_ip="127.64.0.9", secret="a" * 32)
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 3, loopback_ip="127.64.0.1")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 4, secret=f"{1:032x}")
    add_user(conn, 5, tg_id=None)
    add_user(conn, 6, tg_id=None)  # several NULL tg_id allowed


def test_foreign_key_pool_enforced() -> None:
    conn = fresh_db()
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 1, pool_id=99)


def test_check_constraints() -> None:
    conn = fresh_db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pools SET managed = 5")
    uid = add_user(conn, 1, carrier_mode=CarrierMode.WEBSOCKET)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE users SET status='bogus' WHERE id=?", (uid,))


def test_transaction_commit_and_rollback() -> None:
    conn = fresh_db()
    with pytest.raises(RuntimeError), transaction(conn):
        add_user(conn, 1)
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    with transaction(conn):
        add_user(conn, 1)
        with transaction(conn):  # nested joins outer
            add_user(conn, 2)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_snapshot_to(tmp_path: Path) -> None:
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    insert = __import__("tgpanel.db.repo", fromlist=["x"]).insert_pool
    insert(conn, PoolRecord(1, 2400, 8900), NOW)
    add_user(conn, 1)
    dest = tmp_path / "snap.db"
    snapshot_to(conn, dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    copy = connect(dest)
    assert copy.execute("SELECT name FROM users").fetchone()["name"] == "user1"
    assert copy.execute("SELECT status FROM users").fetchone()[0] == UserStatus.ACTIVE.value


async def test_database_async_runner() -> None:
    db = Database()
    from tgpanel.db import repo

    await db.run(repo.insert_pool, PoolRecord(1, 2400, 8900), NOW)
    pools = await db.run(repo.list_pools)
    assert [p.port for p in pools] == [2400]
    db.close()
