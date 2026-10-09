"""Backup creation, listing, retention and DB-snapshot restore helpers (PLAN 3.6 step 1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tgpanel.apply.config import ApplyPaths
from tgpanel.db import repo
from tgpanel.db.connection import Database, current_version
from tgpanel.db.repo import BackupRecord
from tgpanel.system.ops import SystemOps, SystemOpsError

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
    ops: SystemOps, paths: ApplyPaths, *, reason: str, now: datetime, db_snapshot: bytes | None
) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    files_meta: dict[str, dict[str, object]] = {}
    for path in await backup_file_list(ops, paths):
        st = await ops.stat(path)
        members[member_name(path)] = await ops.read_file(path)
        files_meta[member_name(path)] = {"mode": st.mode, "owner": st.owner, "group": st.group}
    if db_snapshot is not None:
        members[DB_MEMBER] = db_snapshot
    manifest = {
        "format": 1,
        "created_at": now.strftime(_TS_FMT),
        "reason": reason,
        "has_db": db_snapshot is not None,
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
    db_snapshot: bytes | None,
) -> BackupInfo:
    clean = sanitize_reason(reason)
    try:
        members = await collect_members(ops, paths, reason=clean, now=now, db_snapshot=db_snapshot)
        dest = await _unique_path(ops, paths.backups_dir, now.strftime(_TS_FMT), clean)
        await ops.make_tar_gz(dest, members)
        size = (await ops.stat(dest)).size
    except SystemOpsError as exc:
        raise BackupError(f"не удалось создать резервную копию: {exc}") from None
    return BackupInfo(dest, now, clean, size)


def db_snapshot_bytes(db: Database) -> bytes:
    """Consistent copy of the SQLite DB via the backup API, read back as bytes."""
    tmp_dir = tempfile.mkdtemp(prefix="tgpanel-snap-")
    try:
        os.chmod(tmp_dir, 0o700)
        target = Path(tmp_dir) / "snapshot.db"
        db.snapshot_to(target)
        # Make the copy a self-contained single file (no WAL sidecars needed to open it).
        copy = sqlite3.connect(str(target))
        try:
            copy.execute("PRAGMA journal_mode = DELETE")
        finally:
            copy.close()
        return target.read_bytes()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    db_snapshot: bytes | None
    file_names: tuple[str, ...]


async def read_archive(ops: SystemOps, path: str) -> BackupArchive:
    try:
        members = await ops.read_tar_gz(path)
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
    names = tuple(sorted(n for n in members if n not in (MANIFEST_NAME, DB_MEMBER)))
    return BackupArchive(manifest, members.get(DB_MEMBER), names)


# Tables restored from a snapshot. History tables (audit_log, apply_runs, backups) keep their
# current content; schema_version is never touched.
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
_SEQUENCE_TABLES = ("users", "access_requests", "broadcasts", "broadcast_items")


def restore_db_snapshot(conn: sqlite3.Connection, snapshot: bytes) -> None:
    """Replace the restorable tables of ``conn`` with the snapshot content.

    Must run inside the caller's transaction (so a later failure rolls everything back).
    The snapshot is validated (integrity, schema version) before any row is touched.
    """
    tmp_dir = tempfile.mkdtemp(prefix="tgpanel-restore-")
    try:
        os.chmod(tmp_dir, 0o700)
        snap_path = Path(tmp_dir) / "snapshot.db"
        snap_path.write_bytes(snapshot)
        os.chmod(snap_path, 0o600)
        try:
            src = sqlite3.connect(str(snap_path))
        except sqlite3.Error:
            raise BackupError("снимок БД в архиве повреждён") from None
        src.row_factory = sqlite3.Row
        try:
            _copy_tables(src, conn)
        except sqlite3.Error:
            raise BackupError("снимок БД в архиве повреждён или несовместим") from None
        finally:
            src.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _copy_tables(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    check = src.execute("PRAGMA integrity_check").fetchone()
    if check is None or str(check[0]) != "ok":
        raise BackupError("снимок БД не прошёл проверку целостности")
    snap_version = current_version_readonly(src)
    if snap_version != current_version(dst):
        raise BackupError("версия схемы снимка БД отличается от текущей")
    dst.execute("PRAGMA defer_foreign_keys = ON")
    for table in reversed(_RESTORE_INSERT_ORDER):
        dst.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    for table in _RESTORE_INSERT_ORDER:
        dst_cols = _columns(dst, table)
        src_cols = set(_columns(src, table))
        cols = [c for c in dst_cols if c in src_cols]
        if not cols:
            continue
        col_sql = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        rows = [tuple(r[c] for c in cols) for r in src.execute(f"SELECT {col_sql} FROM {table}")]  # noqa: S608
        dst.executemany(
            f"INSERT INTO {table} ({col_sql}) VALUES ({marks})",  # noqa: S608
            rows,
        )
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
