"""Backup creation, listing, retention and DB-snapshot restore helpers (PLAN 3.6 step 1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tgpanel.apply.config import ApplyPaths
from tgpanel.db import repo
from tgpanel.db.connection import Database, current_version, migrate
from tgpanel.db.repo import BackupRecord
from tgpanel.system.ops import FileStat, LocalFile, SystemOps, SystemOpsError

MANIFEST_NAME = "MANIFEST.json"
DB_MEMBER = "tgpanel.db"
PRE_INSTALL = "pre-install"
MAX_TREE_DEPTH = 6

_REASON_RE = re.compile(r"[^a-z0-9_-]+")
_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)-([a-z0-9_-]+)(?:\.(\d+))?\.tar\.gz$")
_TS_FMT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """Backup could not be created, read or restored (message is secret-free)."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: str
    created_at: datetime
    reason: str
    size: int
    slim: bool = False


def sanitize_reason(reason: str) -> str:
    cleaned = _REASON_RE.sub("-", reason.strip().lower()).strip("-")
    return cleaned[:40] or "manual"


def member_name(path: str) -> str:
    return path.lstrip("/")


async def _walk(ops: SystemOps, root: str, depth: int = 0) -> list[str]:
    """All regular files under ``root`` (directories are detected by a successful list_dir)."""
    try:
        names = await ops.list_dir(root)
    except SystemOpsError:
        return []
    files: list[str] = []
    for name in names:
        child = f"{root}/{name}"
        try:
            await ops.list_dir(child)
        except SystemOpsError:
            files.append(child)
            continue
        if depth < MAX_TREE_DEPTH:
            files.extend(await _walk(ops, child, depth + 1))
    return files


async def backup_file_list(ops: SystemOps, paths: ApplyPaths) -> list[str]:
    candidates = [paths.config, paths.profiles, paths.mtproxy_env, paths.caddyfile]
    candidates.extend(await _walk(ops, paths.tgpanel_dir))
    try:
        unit_names = await ops.list_dir(paths.systemd_dir)
    except SystemOpsError:
        unit_names = []
    candidates.extend(
        f"{paths.systemd_dir}/{n}" for n in unit_names if n.startswith(("tgpanel-", "tgpanel."))
    )
    out: list[str] = []
    for path in dict.fromkeys(candidates):
        if await ops.exists(path):
            out.append(path)
    return out


async def collect_members(
    ops: SystemOps,
    paths: ApplyPaths,
    *,
    reason: str,
    now: datetime,
    db_file: str | None,
    slim: bool,
    known: Mapping[str, tuple[bytes, FileStat]] | None = None,
) -> dict[str, bytes | LocalFile]:
    """Archive members. ``known`` holds pre-images already read under the lock (not re-read)."""
    known = known or {}
    members: dict[str, bytes | LocalFile] = {}
    files_meta: dict[str, dict[str, object]] = {}
    for path in await backup_file_list(ops, paths):
        if path in known:
            data, st = known[path]
        else:
            st = await ops.stat(path)
            data = await ops.read_file(path)
        members[member_name(path)] = data
        files_meta[member_name(path)] = {"mode": st.mode, "owner": st.owner, "group": st.group}
    for path, (data, st) in known.items():  # pre-images of files that exist but are not listed
        if member_name(path) not in members:
            members[member_name(path)] = data
            files_meta[member_name(path)] = {"mode": st.mode, "owner": st.owner, "group": st.group}
    if db_file is not None:
        members[DB_MEMBER] = LocalFile(db_file)  # streamed into the archive, never read whole
    manifest = {
        "format": 1,
        "created_at": now.strftime(_TS_FMT),
        "reason": reason,
        "has_db": db_file is not None,
        "slim": slim,
        "files": files_meta,
    }
    members[MANIFEST_NAME] = json.dumps(manifest, sort_keys=True, indent=1).encode()
    return members


async def _unique_path(ops: SystemOps, directory: str, stamp: str, reason: str) -> str:
    base = f"{directory}/{stamp}-{reason}"
    candidate = f"{base}.tar.gz"
    n = 1
    while await ops.exists(candidate):
        n += 1
        candidate = f"{base}.{n}.tar.gz"
    return candidate


async def create_backup(
    ops: SystemOps,
    paths: ApplyPaths,
    *,
    reason: str,
    now: datetime,
    db_file: str | None,
    slim: bool = False,
    known: Mapping[str, tuple[bytes, FileStat]] | None = None,
) -> BackupInfo:
    clean = sanitize_reason(reason)
    try:
        members = await collect_members(
            ops, paths, reason=clean, now=now, db_file=db_file, slim=slim, known=known
        )
        dest = await _unique_path(ops, paths.backups_dir, now.strftime(_TS_FMT), clean)
        await ops.make_tar_gz(dest, members)
        size = (await ops.stat(dest)).size
    except SystemOpsError as exc:
        raise BackupError(f"не удалось создать резервную копию: {exc}") from None
    return BackupInfo(dest, now, clean, size, slim)


# ------------------------------------------------------------------------ DB snapshots

# Contents never carried by a slim (pre-apply) snapshot: bulky statistics and history.
SLIM_EXCLUDED = frozenset(
    {
        "traffic_minute",
        "traffic_hour",
        "traffic_day",
        "counter_state",
        "audit_log",
        "apply_runs",
        "backups",
    }
)


def db_file_of(db: Database) -> str | None:
    """Filesystem path of the main database, or None for an in-memory one."""
    rows = db.call(
        lambda c: [str(r[2]) for r in c.execute("PRAGMA database_list") if r[1] == "main"]
    )
    return rows[0] if rows and rows[0] else None


def snapshot_database(db: Database, db_path: str | None, dest: Path, *, slim: bool) -> None:
    """Consistent snapshot of the DB into ``dest`` (a single self-contained 0600 file).

    File databases are read through a SEPARATE read-only connection (WAL snapshot isolation),
    so neither Database's lock nor the writer are involved. The slim variant creates a fresh
    database with the current schema and copies only the small tables via ATTACH (an
    equivalent of VACUUM INTO that skips the traffic/history tables entirely, so its cost does
    not grow with the statistics). In-memory databases (tests) fall back to the backup API
    plus DELETE.
    """
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    if db_path is None:
        db.snapshot_to(dest)
        if slim:
            copy = sqlite3.connect(str(dest))
            try:
                for table in sorted(SLIM_EXCLUDED):
                    copy.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
                copy.commit()
                copy.execute("VACUUM")
            finally:
                copy.close()
        _single_file(dest)
        return
    src_uri = Path(db_path).as_uri() + "?mode=ro"
    if not slim:
        src = sqlite3.connect(src_uri, uri=True)
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    else:
        dst = sqlite3.connect(dest.as_uri(), uri=True, isolation_level=None)
        try:
            dst.row_factory = sqlite3.Row
            migrate(dst)  # schema + schema_version
            dst.execute("ATTACH DATABASE ? AS live", (src_uri,))
            dst.execute("BEGIN")
            tables = [
                str(r[0])
                for r in dst.execute(
                    "SELECT name FROM live.sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'"
                )
                if str(r[0]) not in SLIM_EXCLUDED
            ]
            for table in tables:
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")  # noqa: S608
            dst.execute("DELETE FROM main.sqlite_sequence")
            dst.execute(
                "INSERT INTO main.sqlite_sequence SELECT name, seq FROM live.sqlite_sequence"
            )
            dst.execute("COMMIT")
            dst.execute("DETACH DATABASE live")
        finally:
            dst.close()
    _single_file(dest)


def _single_file(dest: Path) -> None:
    """Make the copy self-contained (no WAL sidecars needed to open it) and private."""
    copy = sqlite3.connect(str(dest))
    try:
        copy.execute("PRAGMA journal_mode = DELETE")
    finally:
        copy.close()
    os.chmod(dest, 0o600)


def make_temp_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="tgpanel-snap-"))
    os.chmod(path, 0o700)
    return path


def remove_temp_dir(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def record_backup(conn: sqlite3.Connection, info: BackupInfo) -> None:
    repo.add_backup(conn, info.path, info.created_at, info.reason, info.size)


# ----------------------------------------------------------------------------- retention


def select_prunable(
    records: Iterable[BackupRecord], now: datetime, keep_last: int, keep_days: int
) -> list[BackupRecord]:
    """Backups to delete: keep the newest ``keep_last`` plus the newest one per UTC day for
    ``keep_days`` days. ``pre-install`` is never rotated."""
    ordered = sorted(records, key=lambda r: (r.created_at, r.id), reverse=True)
    keep: set[int] = {r.id for r in ordered[:keep_last]}
    horizon = now - timedelta(days=keep_days)
    seen_days: set[str] = set()
    for rec in ordered:  # newest first -> first of each day is the newest of that day
        if rec.created_at < horizon:
            continue
        day = rec.created_at.astimezone(UTC).strftime("%Y-%m-%d")
        if day not in seen_days:
            seen_days.add(day)
            keep.add(rec.id)
    return [r for r in ordered if r.id not in keep and r.reason != PRE_INSTALL]


async def prune_backups(
    ops: SystemOps, conn: sqlite3.Connection, now: datetime, keep_last: int, keep_days: int
) -> list[str]:
    removed: list[str] = []
    for rec in select_prunable(repo.list_backups(conn), now, keep_last, keep_days):
        try:
            await ops.remove(rec.path)
        except SystemOpsError:
            continue
        repo.delete_backup(conn, rec.id)
        removed.append(rec.path)
    return removed


async def scan_disk_backups(ops: SystemOps, paths: ApplyPaths) -> list[BackupInfo]:
    """Backups present on disk (e.g. the installer's pre-install), parsed from file names."""
    try:
        names = await ops.list_dir(paths.backups_dir)
    except SystemOpsError:
        return []
    found: list[BackupInfo] = []
    for name in names:
        m = _NAME_RE.match(name)
        path = f"{paths.backups_dir}/{name}"
        if m is None:
            continue
        try:
            created = datetime.strptime(m.group(1), _TS_FMT).replace(tzinfo=UTC)
            size = (await ops.stat(path)).size
        except (ValueError, SystemOpsError):
            continue
        found.append(BackupInfo(path, created, m.group(2), size))
    return found


def add_missing_backups(conn: sqlite3.Connection, infos: Iterable[BackupInfo]) -> int:
    known = {r.path for r in repo.list_backups(conn)}
    added = 0
    for info in infos:
        if info.path not in known:
            record_backup(conn, info)
            added += 1
    return added


# ------------------------------------------------------------------------------ restore


@dataclass(frozen=True, slots=True)
class BackupArchive:
    manifest: dict[str, object]
    has_db: bool
    slim: bool
    file_names: tuple[str, ...]


async def read_archive(ops: SystemOps, path: str) -> BackupArchive:
    """Read only MANIFEST.json (the archive is streamed; nothing else is loaded)."""
    try:
        members = await ops.read_tar_members(path, {MANIFEST_NAME})
    except SystemOpsError as exc:
        raise BackupError(f"не удалось прочитать архив: {exc}") from None
    raw = members.get(MANIFEST_NAME)
    if raw is None:
        raise BackupError("в архиве нет MANIFEST.json: это не резервная копия tgpanel")
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise BackupError("MANIFEST.json повреждён") from None
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise BackupError("неподдерживаемый формат резервной копии")
    files = manifest.get("files")
    names = tuple(sorted(files)) if isinstance(files, dict) else ()
    return BackupArchive(manifest, bool(manifest.get("has_db")), bool(manifest.get("slim")), names)


# Tables restored from a FULL snapshot. History tables (audit_log, apply_runs, backups) keep
# their current content; schema_version is never touched. A SLIM snapshot has no statistics:
# traffic_* and counter_state of surviving users stay as they are.
_RESTORE_INSERT_ORDER = (
    "pools",
    "users",
    "traffic_minute",
    "traffic_hour",
    "traffic_day",
    "counter_state",
    "access_requests",
    "settings",
    "admins",
    "broadcasts",
    "broadcast_items",
)
_STATS_TABLES = frozenset({"traffic_minute", "traffic_hour", "traffic_day", "counter_state"})
_SEQUENCE_TABLES = ("users", "access_requests", "broadcasts", "broadcast_items")


def restore_db_snapshot(conn: sqlite3.Connection, snapshot_path: str, *, slim: bool) -> None:
    """Replace the restorable tables of ``conn`` with the snapshot file's content.

    Must run inside the caller's transaction (so a later failure rolls everything back).
    The snapshot is validated (integrity, schema version) before any row is touched.
    """
    try:
        src = sqlite3.connect(snapshot_path)
    except sqlite3.Error:
        raise BackupError("снимок БД в архиве повреждён") from None
    src.row_factory = sqlite3.Row
    try:
        _copy_tables(src, conn, slim=slim)
    except sqlite3.Error:
        raise BackupError("снимок БД в архиве повреждён или несовместим") from None
    finally:
        src.close()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _rows(
    src: sqlite3.Connection, dst: sqlite3.Connection, table: str
) -> tuple[list[str], list[tuple[object, ...]]]:
    src_cols = set(_columns(src, table))
    cols = [c for c in _columns(dst, table) if c in src_cols]
    if not cols:
        return [], []
    col_sql = ", ".join(cols)
    rows = [tuple(r[c] for c in cols) for r in src.execute(f"SELECT {col_sql} FROM {table}")]  # noqa: S608
    return cols, rows


def _insert(
    dst: sqlite3.Connection, table: str, cols: list[str], rows: list[tuple[object, ...]]
) -> None:
    if not cols:
        return
    marks = ", ".join("?" * len(cols))
    dst.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",  # noqa: S608
        rows,
    )


def _upsert_by_id(
    dst: sqlite3.Connection, table: str, cols: list[str], rows: list[tuple[object, ...]]
) -> None:
    if not cols:
        return
    marks = ", ".join("?" * len(cols))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    dst.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})"  # noqa: S608
        f" ON CONFLICT(id) DO UPDATE SET {updates}",
        rows,
    )


def _copy_tables(src: sqlite3.Connection, dst: sqlite3.Connection, *, slim: bool) -> None:
    check = src.execute("PRAGMA integrity_check").fetchone()
    if check is None or str(check[0]) != "ok":
        raise BackupError("снимок БД не прошёл проверку целостности")
    if current_version_readonly(src) != current_version(dst):
        raise BackupError("версия схемы снимка БД отличается от текущей")
    dst.execute("PRAGMA defer_foreign_keys = ON")
    plain = [
        t for t in _RESTORE_INSERT_ORDER if t not in ("pools", "users") and t not in _STATS_TABLES
    ]
    for table in reversed(plain):
        dst.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    if not slim:
        for table in reversed(_RESTORE_INSERT_ORDER):
            if table in _STATS_TABLES or table in ("pools", "users"):
                dst.execute(f"DELETE FROM {table}")  # noqa: S608
        for table in _RESTORE_INSERT_ORDER:
            if table in ("pools", "users") or table in _STATS_TABLES:
                _insert(dst, table, *_rows(src, dst, table))
    else:
        # Keep the statistics of surviving users: never delete-all `users` (that cascades).
        pool_cols, pool_rows = _rows(src, dst, "pools")
        user_cols, user_rows = _rows(src, dst, "users")
        keep_users = {int(r[user_cols.index("id")]) for r in user_rows}  # type: ignore[call-overload]
        keep_pools = {int(r[pool_cols.index("id")]) for r in pool_rows}  # type: ignore[call-overload]
        for (uid,) in dst.execute("SELECT id FROM users").fetchall():
            if int(uid) not in keep_users:
                dst.execute("DELETE FROM users WHERE id = ?", (uid,))
        for (pid,) in dst.execute("SELECT id FROM pools").fetchall():
            if (
                int(pid) not in keep_pools
                and not dst.execute("SELECT 1 FROM users WHERE pool_id = ?", (pid,)).fetchone()
            ):
                dst.execute("DELETE FROM pools WHERE id = ?", (pid,))
        _upsert_by_id(dst, "pools", pool_cols, pool_rows)
        _upsert_by_id(dst, "users", user_cols, user_rows)
        for (pid,) in dst.execute("SELECT id FROM pools").fetchall():
            if int(pid) not in keep_pools:
                dst.execute("DELETE FROM pools WHERE id = ?", (pid,))
    for table in plain:
        _insert(dst, table, *_rows(src, dst, table))
    for table in _SEQUENCE_TABLES:
        top = dst.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]  # noqa: S608
        cur = dst.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
        seq = max(int(top), int(cur[0]) if cur else 0)
        if cur:
            dst.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (seq, table))
        elif seq:
            dst.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
    violations = dst.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise BackupError("снимок БД нарушает целостность связей")


def current_version_readonly(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)
