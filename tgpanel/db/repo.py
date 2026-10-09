"""Plain repository functions over a sqlite3 connection (parameterized queries only).

Every function takes the connection first. Sortable/filterable columns come from explicit
whitelists; request input is never interpolated into SQL.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from tgpanel.db import times
from tgpanel.db.connection import transaction
from tgpanel.db.times import from_db, from_db_opt, to_db, to_db_opt
from tgpanel.domain.counters import Counter
from tgpanel.domain.models import CarrierMode, PoolRecord, UserRecord, UserStatus
from tgpanel.domain.queries import PER_PAGE_CHOICES, UserListQuery


def _rowid(cur: sqlite3.Cursor) -> int:
    if cur.lastrowid is None:
        raise RuntimeError("insert returned no row id")
    return cur.lastrowid


# --------------------------------------------------------------------------- pools


def pool_from_row(row: sqlite3.Row) -> PoolRecord:
    return PoolRecord(
        id=int(row["id"]),
        port=int(row["port"]),
        stats_port=int(row["stats_port"]),
        managed=bool(row["managed"]),
    )


def insert_pool(conn: sqlite3.Connection, pool: PoolRecord, created_at: datetime) -> None:
    conn.execute(
        "INSERT INTO pools (id, port, stats_port, managed, created_at) VALUES (?, ?, ?, ?, ?)",
        (pool.id, pool.port, pool.stats_port, int(pool.managed), to_db(created_at)),
    )


def list_pools(conn: sqlite3.Connection) -> list[PoolRecord]:
    return [pool_from_row(r) for r in conn.execute("SELECT * FROM pools ORDER BY id")]


def get_pool(conn: sqlite3.Connection, pool_id: int) -> PoolRecord | None:
    row = conn.execute("SELECT * FROM pools WHERE id = ?", (pool_id,)).fetchone()
    return None if row is None else pool_from_row(row)


def delete_pool(conn: sqlite3.Connection, pool_id: int) -> None:
    conn.execute("DELETE FROM pools WHERE id = ?", (pool_id,))


# --------------------------------------------------------------------------- users


def user_from_row(row: sqlite3.Row) -> UserRecord:
    mode = row["carrier_mode"]
    return UserRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        secret=str(row["secret"]),
        status=UserStatus(row["status"]),
        pool_id=int(row["pool_id"]),
        loopback_ip=str(row["loopback_ip"]),
        carrier_mode=None if mode is None else CarrierMode(mode),
        expires_at=from_db_opt(row["expires_at"]),
        comment=str(row["comment"]),
        tg_id=None if row["tg_id"] is None else int(row["tg_id"]),
        imported=bool(row["imported"]),
        source_profile_name=row["source_profile_name"],
    )


@dataclass(frozen=True, slots=True)
class UserExtra:
    """Per-user columns that are not part of the frozen UserRecord."""

    tg_username: str | None
    created_at: datetime
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    can_message: bool
    bot_started: bool
    disabled_reason: str | None


def extra_from_row(row: sqlite3.Row) -> UserExtra:
    return UserExtra(
        tg_username=row["tg_username"],
        created_at=from_db(row["created_at"]),
        first_seen_at=from_db_opt(row["first_seen_at"]),
        last_seen_at=from_db_opt(row["last_seen_at"]),
        can_message=bool(row["can_message"]),
        bot_started=bool(row["bot_started"]),
        disabled_reason=row["disabled_reason"],
    )


def insert_user(
    conn: sqlite3.Connection,
    *,
    name: str,
    secret: str,
    status: UserStatus,
    pool_id: int,
    loopback_ip: str,
    created_at: datetime,
    carrier_mode: CarrierMode | None = None,
    expires_at: datetime | None = None,
    comment: str = "",
    tg_id: int | None = None,
    tg_username: str | None = None,
    imported: bool = False,
    source_profile_name: str | None = None,
    can_message: bool = False,
    bot_started: bool = False,
    disabled_reason: str | None = None,
    first_seen_at: datetime | None = None,
    last_seen_at: datetime | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO users (name, comment, tg_id, tg_username, secret, status, disabled_reason,"
        " pool_id, loopback_ip, carrier_mode, created_at, expires_at, can_message, bot_started,"
        " imported, source_profile_name, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            comment,
            tg_id,
            tg_username,
            secret,
            status.value,
            disabled_reason,
            pool_id,
            loopback_ip,
            None if carrier_mode is None else carrier_mode.value,
            to_db(created_at),
            to_db_opt(expires_at),
            int(can_message),
            int(bot_started),
            int(imported),
            source_profile_name,
            to_db_opt(first_seen_at),
            to_db_opt(last_seen_at),
        ),
    )
    return _rowid(cur)


def get_user(conn: sqlite3.Connection, user_id: int) -> UserRecord | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else user_from_row(row)


def get_user_extra(conn: sqlite3.Connection, user_id: int) -> UserExtra | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else extra_from_row(row)


def get_user_by_tg_id(conn: sqlite3.Connection, tg_id: int) -> UserRecord | None:
    row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return None if row is None else user_from_row(row)


def get_user_by_secret(conn: sqlite3.Connection, secret: str) -> UserRecord | None:
    row = conn.execute("SELECT * FROM users WHERE secret = ?", (secret,)).fetchone()
    return None if row is None else user_from_row(row)


def all_users(conn: sqlite3.Connection) -> list[UserRecord]:
    return [user_from_row(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


def users_by_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> list[UserRecord]:
    out: list[UserRecord] = []
    for i in range(0, len(ids), 500):
        chunk = list(ids[i : i + 500])
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM users WHERE id IN ({marks}) ORDER BY id",  # noqa: S608
            chunk,
        )
        out.extend(user_from_row(r) for r in rows)
    return out


def used_loopback_ips(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT loopback_ip FROM users")]


_UPDATABLE = frozenset(
    {
        "name",
        "comment",
        "tg_id",
        "tg_username",
        "secret",
        "status",
        "disabled_reason",
        "pool_id",
        "loopback_ip",
        "carrier_mode",
        "expires_at",
        "first_seen_at",
        "last_seen_at",
        "can_message",
        "bot_started",
    }
)


def _to_sql(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return to_db(value)
    if isinstance(value, bool):
        return int(value)
    return value


def update_user(conn: sqlite3.Connection, user_id: int, **fields: Any) -> None:
    """Update whitelisted columns of one user; unknown column names raise ValueError."""
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update columns: {sorted(unknown)}")
    sets = ", ".join(f"{col} = ?" for col in fields)  # columns are whitelisted above
    conn.execute(
        f"UPDATE users SET {sets} WHERE id = ?",  # noqa: S608
        [*(_to_sql(v) for v in fields.values()), user_id],
    )


def delete_users(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Delete users; traffic and counter state go with them (ON DELETE CASCADE)."""
    conn.executemany("DELETE FROM users WHERE id = ?", [(i,) for i in ids])


def pool_occupancy(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT p.id AS id, COUNT(u.id) AS n FROM pools p LEFT JOIN users u ON u.pool_id = p.id"
        " GROUP BY p.id"
    )
    return {int(r["id"]): int(r["n"]) for r in rows}


# ------------------------------------------------------------------ user list query

ONLINE_FRESHNESS = timedelta(seconds=90)


def _online_sql(cutoff: datetime) -> str:
    """Online = active in two last polls AND counter_state refreshed within the window.

    The cutoff literal comes from ``to_db`` (fixed-width digits), never from user input.
    """
    return (
        "(COALESCE(cs.active_last, 0) = 1 AND COALESCE(cs.active_prev, 0) = 1"
        f" AND COALESCE(cs.updated_at, '') >= '{to_db(cutoff)}')"
    )


_TRAFFIC = "(COALESCE(t.up, 0) + COALESCE(t.down, 0))"

# sort name -> (SQL expression, nullable, text). Never built from request input.
SORT_EXPRESSIONS: dict[str, tuple[str, bool, bool]] = {
    "id": ("u.id", False, False),
    "name": ("u.name", False, True),
    "comment": ("u.comment", False, True),
    "tg_id": ("u.tg_id", True, False),
    "tg_username": ("u.tg_username", True, True),
    "status": ("u.status", False, False),
    "online": ("", False, False),  # built per call: depends on the freshness cutoff
    "created_at": ("u.created_at", False, False),
    "expires_at": ("u.expires_at", True, False),
    "first_seen_at": ("u.first_seen_at", True, False),
    "last_seen_at": ("u.last_seen_at", True, False),
    "traffic": (_TRAFFIC, False, False),
    "pool_id": ("u.pool_id", False, False),
    "bot_started": ("u.bot_started", False, False),
}

PERIOD_DELTAS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}

TRAFFIC_TIERS: dict[str, str] = {
    "minute": "traffic_minute",
    "hour": "traffic_hour",
    "day": "traffic_day",
}
_TIER_SECONDS = {"hour": 3600, "day": 86400}


@dataclass(frozen=True, slots=True)
class UserListRow:
    user: UserRecord
    extra: UserExtra
    online: bool
    bytes_up: int
    bytes_down: int

    @property
    def first_seen_at(self) -> datetime | None:
        return self.extra.first_seen_at

    @property
    def last_seen_at(self) -> datetime | None:
        return self.extra.last_seen_at


def _like(text: str) -> str:
    esc = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _py_lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _traffic_for_users(
    conn: sqlite3.Connection, ids: Sequence[int], since: datetime | None
) -> dict[int, tuple[int, int]]:
    """(up, down) per user id over the three tiers, for the given ids only."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    parts: list[str] = []
    params: list[Any] = []
    for table in TRAFFIC_TIERS.values():
        sql = f"SELECT user_id, bytes_up, bytes_down FROM {table} WHERE user_id IN ({marks})"  # noqa: S608
        params += list(ids)
        if since is not None:
            sql += " AND bucket_ts >= ?"
            params.append(times.to_epoch(since))
        parts.append(sql)
    rows = conn.execute(
        "SELECT user_id, SUM(bytes_up) AS up, SUM(bytes_down) AS down FROM ("  # noqa: S608
        + " UNION ALL ".join(parts)
        + ") GROUP BY user_id",
        params,
    )
    return {int(r["user_id"]): (int(r["up"]), int(r["down"])) for r in rows}


def _online_for_users(
    conn: sqlite3.Connection, ids: Sequence[int], cutoff: datetime
) -> dict[int, bool]:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT user_id, active_last, active_prev, updated_at FROM counter_state"  # noqa: S608
        f" WHERE user_id IN ({','.join('?' * len(ids))})",
        list(ids),
    )
    result: dict[int, bool] = {}
    for r in rows:
        updated = from_db_opt(r["updated_at"])
        result[int(r["user_id"])] = bool(
            r["active_last"] and r["active_prev"] and updated is not None and updated >= cutoff
        )
    return result


def list_users(
    conn: sqlite3.Connection,
    query: UserListQuery,
    now: datetime,
    online_freshness: timedelta = ONLINE_FRESHNESS,
) -> tuple[list[UserListRow], int]:
    """Filtered, sorted, paginated user list with traffic over ``query.period``.

    Traffic is the sum over the minute, hour and day tiers (tiers are non-overlapping:
    rollup moves data). Traffic/counter tables are joined only when sorting or filtering
    needs them; otherwise the page is selected first and traffic/online are computed for
    just its ids. A user is online only if counter_state was refreshed within
    ``online_freshness`` of ``now`` (a dead collector means nobody is online).
    Returns (rows of the requested page, total matching count).
    """
    if query.sort not in SORT_EXPRESSIONS:
        raise ValueError(f"unknown sort field: {query.sort!r}")
    if query.period not in PERIOD_DELTAS:
        raise ValueError(f"unknown period: {query.period!r}")
    if query.page < 1:
        raise ValueError("page must be >= 1")
    if query.per_page not in PER_PAGE_CHOICES:
        raise ValueError(f"per_page must be one of {PER_PAGE_CHOICES}")
    conn.create_function("py_lower", 1, _py_lower, deterministic=True)

    f = query.filter
    cutoff = now - online_freshness
    online_sql = _online_sql(cutoff)
    delta = PERIOD_DELTAS[query.period]
    since = None if delta is None else now - delta

    need_traffic = query.sort == "traffic" or f.traffic_min is not None or f.traffic_max is not None
    need_cs = query.sort == "online" or f.online is not None

    from_sql = "FROM users u"
    tier_params: list[Any] = []
    if need_cs:
        from_sql += " LEFT JOIN counter_state cs ON cs.user_id = u.id"
    if need_traffic:
        parts: list[str] = []
        for table in TRAFFIC_TIERS.values():
            if since is None:
                parts.append(f"SELECT user_id, bytes_up, bytes_down FROM {table}")  # noqa: S608
            else:
                parts.append(
                    f"SELECT user_id, bytes_up, bytes_down FROM {table} WHERE bucket_ts >= ?"  # noqa: S608
                )
                tier_params.append(times.to_epoch(since))
        traffic = (
            "SELECT user_id, SUM(bytes_up) AS up, SUM(bytes_down) AS down FROM ("  # noqa: S608
            + " UNION ALL ".join(parts)
            + ") GROUP BY user_id"
        )
        from_sql += f" LEFT JOIN ({traffic}) t ON t.user_id = u.id"

    where: list[str] = []
    params: list[Any] = []
    if f.query:
        pat = _like(f.query)
        where.append(
            "(py_lower(u.name) LIKE ? ESCAPE '\\' OR py_lower(u.comment) LIKE ? ESCAPE '\\'"
            " OR CAST(u.tg_id AS TEXT) LIKE ? ESCAPE '\\'"
            " OR py_lower(COALESCE(u.tg_username, '')) LIKE ? ESCAPE '\\')"
        )
        params += [pat, pat, pat, pat]
    if f.statuses:
        where.append(f"u.status IN ({','.join('?' * len(f.statuses))})")
        params += [UserStatus(s).value for s in f.statuses]
    if f.online is not None:
        where.append(online_sql if f.online else f"NOT {online_sql}")
    if f.imported is not None:
        where.append("u.imported = ?")
        params.append(int(f.imported))
    if f.has_tg_id is not None:
        where.append("u.tg_id IS NOT NULL" if f.has_tg_id else "u.tg_id IS NULL")
    if f.bot_started is not None:
        where.append("u.bot_started = ?")
        params.append(int(f.bot_started))
    if f.expires_within_days is not None:
        where.append("(u.expires_at IS NOT NULL AND u.expires_at >= ? AND u.expires_at <= ?)")
        params += [to_db(now), to_db(now + timedelta(days=f.expires_within_days))]
    if f.created_from is not None:
        where.append("u.created_at >= ?")
        params.append(to_db(f.created_from))
    if f.created_to is not None:
        where.append("u.created_at <= ?")
        params.append(to_db(f.created_to))
    if f.last_seen_from is not None:
        where.append("u.last_seen_at >= ?")
        params.append(to_db(f.last_seen_from))
    if f.last_seen_to is not None:
        where.append("u.last_seen_at <= ?")
        params.append(to_db(f.last_seen_to))
    if f.traffic_min is not None:
        where.append(f"{_TRAFFIC} >= ?")
        params.append(f.traffic_min)
    if f.traffic_max is not None:
        where.append(f"{_TRAFFIC} <= ?")
        params.append(f.traffic_max)
    if f.comment_contains:
        where.append("py_lower(u.comment) LIKE ? ESCAPE '\\'")
        params.append(_like(f.comment_contains))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    expr, nullable, text = SORT_EXPRESSIONS[query.sort]
    if query.sort == "online":
        expr = online_sql
    if text:
        expr = f"py_lower({expr})"
    direction = "DESC" if query.descending else "ASC"
    order = f"{expr} {direction}"
    if nullable:
        order = f"({expr} IS NULL) ASC, {order}"
    order_sql = f" ORDER BY {order}, u.id ASC"

    total = int(
        conn.execute(
            f"SELECT COUNT(*) {from_sql}{where_sql}",
            [*tier_params, *params],
        ).fetchone()[0]
    )
    limit_params = [query.per_page, (query.page - 1) * query.per_page]
    page = conn.execute(
        f"SELECT u.* {from_sql}{where_sql}{order_sql} LIMIT ? OFFSET ?",
        [*tier_params, *params, *limit_params],
    ).fetchall()
    ids = [int(r["id"]) for r in page]
    traffic_by_id = _traffic_for_users(conn, ids, since)
    online_by_id = _online_for_users(conn, ids, cutoff)
    rows = [
        UserListRow(
            user=user_from_row(r),
            extra=extra_from_row(r),
            online=online_by_id.get(int(r["id"]), False),
            bytes_up=traffic_by_id.get(int(r["id"]), (0, 0))[0],
            bytes_down=traffic_by_id.get(int(r["id"]), (0, 0))[1],
        )
        for r in page
    ]
    return rows, total


# ------------------------------------------------------------------------ settings


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT * FROM settings")}


# -------------------------------------------------------------------------- admins


def add_admin(conn: sqlite3.Connection, tg_id: int, added_at: datetime) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO admins (tg_id, added_at) VALUES (?, ?)", (tg_id, to_db(added_at))
    )


def remove_admin(conn: sqlite3.Connection, tg_id: int) -> None:
    conn.execute("DELETE FROM admins WHERE tg_id = ?", (tg_id,))


def list_admins(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT tg_id FROM admins ORDER BY tg_id")]


def is_admin(conn: sqlite3.Connection, tg_id: int) -> bool:
    return conn.execute("SELECT 1 FROM admins WHERE tg_id = ?", (tg_id,)).fetchone() is not None


# ----------------------------------------------------------------------- audit log


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    ts: datetime
    actor: str
    action: str
    target: str
    details: str


_SECRET_LIKE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")


def _redact(text: str) -> str:
    """Replace anything that looks like a proxy secret (32 hex, optionally dd-prefixed)."""
    return _SECRET_LIKE.sub("[redacted]", text)


def add_audit(
    conn: sqlite3.Connection,
    ts: datetime,
    actor: str,
    action: str,
    target: str = "",
    details: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, details) VALUES (?, ?, ?, ?, ?)",
        (to_db(ts), actor, action, _redact(target), _redact(details)),
    )
    return _rowid(cur)


def list_audit(
    conn: sqlite3.Connection, *, target: str | None = None, limit: int = 100, offset: int = 0
) -> list[AuditEntry]:
    if target is None:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE target = ? ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (target, limit, offset),
        )
    return [
        AuditEntry(
            id=int(r["id"]),
            ts=from_db(r["ts"]),
            actor=str(r["actor"]),
            action=str(r["action"]),
            target=str(r["target"]),
            details=str(r["details"]),
        )
        for r in rows
    ]


# --------------------------------------------------------------------- apply runs


@dataclass(frozen=True, slots=True)
class ApplyRun:
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    reason: str
    backup_path: str | None
    error: str | None


def _apply_run(r: sqlite3.Row) -> ApplyRun:
    return ApplyRun(
        id=int(r["id"]),
        started_at=from_db(r["started_at"]),
        finished_at=from_db_opt(r["finished_at"]),
        status=str(r["status"]),
        reason=str(r["reason"]),
        backup_path=r["backup_path"],
        error=r["error"],
    )


def start_apply_run(
    conn: sqlite3.Connection, started_at: datetime, reason: str, backup_path: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO apply_runs (started_at, status, reason, backup_path)"
        " VALUES (?, 'running', ?, ?)",
        (to_db(started_at), reason, backup_path),
    )
    return _rowid(cur)


def finish_apply_run(
    conn: sqlite3.Connection,
    run_id: int,
    finished_at: datetime,
    status: str,
    *,
    error: str | None = None,
    backup_path: str | None = None,
) -> None:
    conn.execute(
        "UPDATE apply_runs SET finished_at = ?, status = ?, error = ?,"
        " backup_path = COALESCE(?, backup_path) WHERE id = ?",
        (to_db(finished_at), status, error, backup_path, run_id),
    )


def get_apply_run(conn: sqlite3.Connection, run_id: int) -> ApplyRun | None:
    row = conn.execute("SELECT * FROM apply_runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _apply_run(row)


def list_apply_runs(conn: sqlite3.Connection, limit: int = 50, offset: int = 0) -> list[ApplyRun]:
    rows = conn.execute(
        "SELECT * FROM apply_runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    )
    return [_apply_run(r) for r in rows]


# ----------------------------------------------------------------------- backups


@dataclass(frozen=True, slots=True)
class BackupRecord:
    id: int
    path: str
    created_at: datetime
    reason: str
    size: int


def _backup(r: sqlite3.Row) -> BackupRecord:
    return BackupRecord(
        id=int(r["id"]),
        path=str(r["path"]),
        created_at=from_db(r["created_at"]),
        reason=str(r["reason"]),
        size=int(r["size"]),
    )


def add_backup(
    conn: sqlite3.Connection, path: str, created_at: datetime, reason: str, size: int
) -> int:
    cur = conn.execute(
        "INSERT INTO backups (path, created_at, reason, size) VALUES (?, ?, ?, ?)",
        (path, to_db(created_at), reason, size),
    )
    return _rowid(cur)


def get_backup(conn: sqlite3.Connection, backup_id: int) -> BackupRecord | None:
    row = conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
    return None if row is None else _backup(row)


def list_backups(conn: sqlite3.Connection) -> list[BackupRecord]:
    return [
        _backup(r) for r in conn.execute("SELECT * FROM backups ORDER BY created_at DESC, id DESC")
    ]


def delete_backup(conn: sqlite3.Connection, backup_id: int) -> None:
    conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))


# ------------------------------------------------------------------ access requests


@dataclass(frozen=True, slots=True)
class AccessRequest:
    id: int
    tg_id: int
    tg_username: str | None
    full_name: str
    status: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None


def _request(r: sqlite3.Row) -> AccessRequest:
    return AccessRequest(
        id=int(r["id"]),
        tg_id=int(r["tg_id"]),
        tg_username=r["tg_username"],
        full_name=str(r["full_name"]),
        status=str(r["status"]),
        created_at=from_db(r["created_at"]),
        decided_by=r["decided_by"],
        decided_at=from_db_opt(r["decided_at"]),
    )


def create_access_request(
    conn: sqlite3.Connection,
    tg_id: int,
    tg_username: str | None,
    full_name: str,
    created_at: datetime,
) -> int:
    """Raises sqlite3.IntegrityError if the tg_id already has a pending request."""
    cur = conn.execute(
        "INSERT INTO access_requests (tg_id, tg_username, full_name, status, created_at)"
        " VALUES (?, ?, ?, 'pending', ?)",
        (tg_id, tg_username, full_name, to_db(created_at)),
    )
    return _rowid(cur)


def get_access_request(conn: sqlite3.Connection, request_id: int) -> AccessRequest | None:
    row = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    return None if row is None else _request(row)


def pending_request_for(conn: sqlite3.Connection, tg_id: int) -> AccessRequest | None:
    row = conn.execute(
        "SELECT * FROM access_requests WHERE tg_id = ? AND status = 'pending'", (tg_id,)
    ).fetchone()
    return None if row is None else _request(row)


def list_access_requests(
    conn: sqlite3.Connection, status: str | None = None
) -> list[AccessRequest]:
    if status is None:
        rows = conn.execute("SELECT * FROM access_requests ORDER BY created_at DESC, id DESC")
    else:
        rows = conn.execute(
            "SELECT * FROM access_requests WHERE status = ? ORDER BY created_at DESC, id DESC",
            (status,),
        )
    return [_request(r) for r in rows]


def decide_access_request(
    conn: sqlite3.Connection,
    request_id: int,
    status: str,
    decided_by: str,
    decided_at: datetime,
) -> None:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    conn.execute(
        "UPDATE access_requests SET status = ?, decided_by = ?, decided_at = ?"
        " WHERE id = ? AND status = 'pending'",
        (status, decided_by, to_db(decided_at), request_id),
    )


# ---------------------------------------------------------------------- broadcasts


def create_broadcast(
    conn: sqlite3.Connection, created_by: str, created_at: datetime, template: str
) -> int:
    cur = conn.execute(
        "INSERT INTO broadcasts (created_by, created_at, template) VALUES (?, ?, ?)",
        (created_by, to_db(created_at), template),
    )
    return _rowid(cur)


def add_broadcast_item(
    conn: sqlite3.Connection, broadcast_id: int, user_id: int | None, tg_id: int | None
) -> int:
    cur = conn.execute(
        "INSERT INTO broadcast_items (broadcast_id, user_id, tg_id) VALUES (?, ?, ?)",
        (broadcast_id, user_id, tg_id),
    )
    return _rowid(cur)


def set_broadcast_item_result(
    conn: sqlite3.Connection, item_id: int, result: str, sent_at: datetime | None
) -> None:
    conn.execute(
        "UPDATE broadcast_items SET result = ?, sent_at = ? WHERE id = ?",
        (result, to_db_opt(sent_at), item_id),
    )


def list_broadcast_items(conn: sqlite3.Connection, broadcast_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM broadcast_items WHERE broadcast_id = ? ORDER BY id", (broadcast_id,)
        )
    )


# ------------------------------------------------------------------------- traffic


@dataclass(frozen=True, slots=True)
class TrafficPoint:
    ts: datetime
    bytes_up: int
    bytes_down: int
    packets_up: int
    packets_down: int


def _tier_table(tier: str) -> str:
    try:
        return TRAFFIC_TIERS[tier]
    except KeyError:
        raise ValueError(f"unknown traffic tier: {tier!r}") from None


def add_traffic(
    conn: sqlite3.Connection,
    tier: str,
    user_id: int,
    bucket: datetime,
    *,
    bytes_up: int = 0,
    bytes_down: int = 0,
    packets_up: int = 0,
    packets_down: int = 0,
) -> None:
    """Add to a bucket (upsert, additive). ``bucket`` must already be floored by the caller."""
    table = _tier_table(tier)
    conn.execute(
        f"INSERT INTO {table} (user_id, bucket_ts, bytes_up, bytes_down, packets_up,"  # noqa: S608
        " packets_down) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, bucket_ts) DO UPDATE SET"
        " bytes_up = bytes_up + excluded.bytes_up, bytes_down = bytes_down + excluded.bytes_down,"
        " packets_up = packets_up + excluded.packets_up,"
        " packets_down = packets_down + excluded.packets_down",
        (user_id, times.to_epoch(bucket), bytes_up, bytes_down, packets_up, packets_down),
    )


def get_traffic(
    conn: sqlite3.Connection,
    tier: str,
    user_id: int,
    start: datetime,
    end: datetime,
) -> list[TrafficPoint]:
    """Buckets with start <= ts < end, ascending."""
    table = _tier_table(tier)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE user_id = ? AND bucket_ts >= ? AND bucket_ts < ?"  # noqa: S608
        " ORDER BY bucket_ts",
        (user_id, times.to_epoch(start), times.to_epoch(end)),
    )
    return [
        TrafficPoint(
            ts=times.from_epoch(int(r["bucket_ts"])),
            bytes_up=int(r["bytes_up"]),
            bytes_down=int(r["bytes_down"]),
            packets_up=int(r["packets_up"]),
            packets_down=int(r["packets_down"]),
        )
        for r in rows
    ]


def traffic_totals(
    conn: sqlite3.Connection, user_id: int, start: datetime | None = None
) -> tuple[int, int]:
    """(bytes_up, bytes_down) over all tiers from ``start`` (None = all time)."""
    up = down = 0
    for table in TRAFFIC_TIERS.values():
        if start is None:
            row = conn.execute(
                f"SELECT COALESCE(SUM(bytes_up), 0), COALESCE(SUM(bytes_down), 0)"  # noqa: S608
                f" FROM {table} WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT COALESCE(SUM(bytes_up), 0), COALESCE(SUM(bytes_down), 0)"  # noqa: S608
                f" FROM {table} WHERE user_id = ? AND bucket_ts >= ?",
                (user_id, times.to_epoch(start)),
            ).fetchone()
        up += int(row[0])
        down += int(row[1])
    return up, down


def delete_traffic_before(conn: sqlite3.Connection, tier: str, before: datetime) -> int:
    table = _tier_table(tier)
    cur = conn.execute(
        f"DELETE FROM {table} WHERE bucket_ts < ?",  # noqa: S608
        (times.to_epoch(before),),
    )
    return cur.rowcount


def rollup(conn: sqlite3.Connection, src: str, dst: str, before: datetime) -> int:
    """Move buckets older than ``before`` from ``src`` into ``dst`` (minute->hour->day).

    Atomic; returns the number of source rows moved.
    """
    if (src, dst) not in (("minute", "hour"), ("hour", "day")):
        raise ValueError("rollup supports minute->hour and hour->day only")
    src_t, dst_t = TRAFFIC_TIERS[src], TRAFFIC_TIERS[dst]
    size = _TIER_SECONDS[dst]
    cutoff = times.to_epoch(before)
    with transaction(conn):
        conn.execute(
            f"INSERT INTO {dst_t} (user_id, bucket_ts, bytes_up, bytes_down, packets_up,"  # noqa: S608
            " packets_down)"
            f" SELECT user_id, (bucket_ts / {int(size)}) * {int(size)} AS b, SUM(bytes_up),"
            " SUM(bytes_down), SUM(packets_up), SUM(packets_down)"
            f" FROM {src_t} WHERE bucket_ts < ? GROUP BY user_id, b"
            " ON CONFLICT(user_id, bucket_ts) DO UPDATE SET"
            " bytes_up = bytes_up + excluded.bytes_up,"
            " bytes_down = bytes_down + excluded.bytes_down,"
            " packets_up = packets_up + excluded.packets_up,"
            " packets_down = packets_down + excluded.packets_down",
            (cutoff,),
        )
        cur = conn.execute(f"DELETE FROM {src_t} WHERE bucket_ts < ?", (cutoff,))  # noqa: S608
    return cur.rowcount


# ------------------------------------------------------------------ counter state


@dataclass(frozen=True, slots=True)
class CounterStateRow:
    user_id: int
    up: Counter
    down: Counter
    active_last: bool
    active_prev: bool
    updated_at: datetime | None


def get_counter_state(conn: sqlite3.Connection, user_id: int) -> CounterStateRow | None:
    r = conn.execute("SELECT * FROM counter_state WHERE user_id = ?", (user_id,)).fetchone()
    if r is None:
        return None
    return CounterStateRow(
        user_id=int(r["user_id"]),
        up=Counter(int(r["last_up"]), int(r["last_packets_up"])),
        down=Counter(int(r["last_down"]), int(r["last_packets_down"])),
        active_last=bool(r["active_last"]),
        active_prev=bool(r["active_prev"]),
        updated_at=from_db_opt(r["updated_at"]),
    )


def put_counter_state(conn: sqlite3.Connection, row: CounterStateRow) -> None:
    conn.execute(
        "INSERT INTO counter_state (user_id, last_up, last_down, last_packets_up,"
        " last_packets_down, active_last, active_prev, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET last_up = excluded.last_up,"
        " last_down = excluded.last_down, last_packets_up = excluded.last_packets_up,"
        " last_packets_down = excluded.last_packets_down, active_last = excluded.active_last,"
        " active_prev = excluded.active_prev, updated_at = excluded.updated_at",
        (
            row.user_id,
            row.up.bytes,
            row.down.bytes,
            row.up.packets,
            row.down.packets,
            int(row.active_last),
            int(row.active_prev),
            to_db_opt(row.updated_at),
        ),
    )


def delete_counter_state(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM counter_state WHERE user_id = ?", (user_id,))
