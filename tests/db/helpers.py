"""Builders for DB tests (synthetic data only)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from tgpanel.db.connection import open_database
from tgpanel.db.repo import insert_pool, insert_user
from tgpanel.domain.models import PoolRecord, UserStatus

NOW = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)


def fresh_db() -> sqlite3.Connection:
    conn = open_database(":memory:")
    insert_pool(conn, PoolRecord(id=1, port=2400, stats_port=8900), NOW)
    insert_pool(conn, PoolRecord(id=2, port=2401, stats_port=8901), NOW)
    return conn


def add_user(conn: sqlite3.Connection, i: int, **kw: Any) -> int:
    params: dict[str, Any] = {
        "name": f"user{i}",
        "secret": f"{i:032x}",
        "status": UserStatus.ACTIVE,
        "pool_id": 1,
        "loopback_ip": f"127.64.0.{i}",
        "created_at": NOW,
    }
    params.update(kw)
    return insert_user(conn, **params)
