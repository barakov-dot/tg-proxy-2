"""The apply pipeline (PLAN 3.6): the only code allowed to change proxy-side files.

One operation = DB mutation + apply, sharing one transaction boundary:

    flock -> [drift check] -> BEGIN (own connection) -> mutations (SAVEPOINT each)
          -> render -> validate (-check on temp files) -> backup -> write files
          -> pools (start/restart, wait port) -> nft element diff -> config/profiles
          -> relay restart -> /healthz -> one /readyz -> stop emptied pools -> COMMIT

Any failure restores every touched file from the pre-images captured under the lock (the same
bytes the step-1 backup archives), reloads systemd, restarts what was restarted, reverts nft
elements, and ROLLBACKs the DB, so the operation counts as not performed.

Operations are strictly serialised. Operations submitted while an apply runs are queued and
all of them are executed (mutations) and applied together in ONE next apply.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tgpanel.apply import backup as backup_mod
from tgpanel.apply.backup import BackupError, BackupInfo
from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.errors import (
    ApplyError,
    DbWriteTimeout,
    ExternalChangeDetected,
    OperationRejected,
    SettingsError,
)
from tgpanel.apply.settings_spec import (
    KEY_ADOPTED,
    KEY_ALL_NAMES,
    KEY_MTPROXY_FACTS,
    KEY_OUR_NAMES,
    KEY_PANEL_SESSION_VERSION,
    KEY_PROFILES_HASH,
    read_settings,
)
from tgpanel.apply.state import (
    facts_to_json,
    load_desired_state,
    read_live_facts,
    resolve_facts,
    split_foreign,
    stored_names,
)
from tgpanel.db import repo
from tgpanel.db.connection import Database, connect, transaction
from tgpanel.domain.invariants import capacity_warnings, validate_desired_state
from tgpanel.domain.models import DesiredState, PoolRecord, UserStatus
from tgpanel.render.bundle import RenderedFiles, render_all
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import (
    SENTINEL_NAME,
    active_users,
    foreign_loopback_ips,
    parse_profiles,
    profiles_hash,
)
from tgpanel.system.ops import FileStat, NftTableMissing, SystemOps, SystemOpsError
from tgpanel.system.validation import scrub

log = logging.getLogger(__name__)

_USER_PROFILE_RE = re.compile(r"u[1-9][0-9]*")
_POOL_ENV_RE = re.compile(r"^([0-9]+)\.env$")
_TEMP_NAME_RE = re.compile(r"^\.tgpanel-.*(\.tmp)?$")
PROFILES_MODE = 0o400
CHECK_COPY_MODE = 0o600
LEGACY_OFF_DROPIN = "[Unit]\nConditionPathExists=/nonexistent-tgpanel-disabled\n"

BATCH_FAILED_NOTE = "Групповое применение не удалось, повтор по отдельности тоже: "

STAGE_RU = {
    "lock": "блокировка применения",
    "backup": "резервная копия",
    "render": "подготовка конфигурации",
    "validate": "проверка конфигурации",
    "write": "запись файлов",
    "pool": "запуск MTProxy",
    "nft": "правила nftables",
    "relay": "перезапуск relay",
    "health": "проверка работоспособности",
    "commit": "сохранение в базе данных",
    "internal": "внутренняя ошибка",
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _clean(text: str) -> str:
    return scrub(text, 700)


# ------------------------------------------------------------------------------- results


@dataclass(slots=True)
class OperationOutcome[T]:
    """Result of one submitted operation."""

    ok: bool
    status: str  # applied | noop | failed | rejected | external_change
    value: T | None = None
    error: str | None = None  # Russian, secret-free
    apply_run_id: int | None = None
    external: ExternalChangeDetected | None = None
    rolled_back: bool = False
    rollback_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class RecoveryReport:
    """What ``startup_recovery`` did."""

    cleaned_temp_files: int = 0
    run_ids: list[int] = field(default_factory=list)
    restored_files: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DriftReport:
    description: str
    no_baseline: bool = False

    def exception(self) -> ExternalChangeDetected:
        return ExternalChangeDetected(self.description, no_baseline=self.no_baseline)


@dataclass(frozen=True, slots=True)
class ApplyFailure:
    """Passed to the optional ``on_failure`` hook (bot notification, banner)."""

    run_id: int
    reason: str
    actor: str
    error: str
    rolled_back: bool
    rollback_errors: tuple[str, ...]


# ------------------------------------------------------------------------------ internals


@dataclass(eq=False)
class _Submission:
    mutation: Callable[[sqlite3.Connection], Any]
    reason: str
    actor: str
    force_external: bool
    expect_hash: str | None
    full_nft_reload: bool
    future: asyncio.Future[OperationOutcome[Any]]
    bypass_adoption: bool = False  # restore: the DB is about to be replaced on purpose
    prune_orphans: bool = False  # restore / explicit prune: stop pools unknown to the DB


@dataclass(frozen=True, slots=True)
class _Snap:
    data: bytes | None = field(repr=False)
    stat: FileStat | None


@dataclass(frozen=True, slots=True)
class _Target:
    path: str
    data: bytes = field(repr=False)
    mode: int
    owner: str
    group: str


@dataclass(slots=True)
class _Journal:
    written: list[str] = field(default_factory=list)
    unit_written: bool = False
    pools: dict[int, bool] = field(default_factory=dict)  # pool id -> was active before
    stopped: list[int] = field(default_factory=list)
    nft_added: list[tuple[str, list[str]]] = field(default_factory=list)
    nft_deleted: list[tuple[str, list[str]]] = field(default_factory=list)
    nft_loaded: bool = False  # the whole table was (re)loaded from the file
    relay_touched: bool = False


@dataclass(slots=True)
class _Plan:
    state: DesiredState
    rendered: RenderedFiles
    targets: dict[str, _Target]
    snaps: dict[str, _Snap]
    changed: set[str]
    pools_by_id: dict[int, PoolRecord]
    to_start: list[tuple[PoolRecord, bool]]  # (pool, was active)
    to_stop: list[int]  # active pools without secrets (stopped after the relay is healthy)
    stale_envs: list[str]  # env files of stopped/orphan pools, removed after success
    nft_full: bool
    nft_changes: list[tuple[str, list[str], list[str]]]  # (set, to_add, to_delete)
    relay_restart: bool
    warnings: list[str]
    nft_table_existed: bool = True
    # IPs whose owner changed since the last apply: element is deleted and re-added so its
    # counters restart from zero (the new owner must not inherit the old traffic).
    nft_recreate: list[str] = field(default_factory=list)

    @property
    def noop(self) -> bool:
        return not (
            self.changed
            or self.to_start
            or self.to_stop
            or self.nft_full
            or any(add or delete for _, add, delete in self.nft_changes)
            or bool(self.nft_recreate)
            or self.relay_restart
        )


KEY_NFT_OWNERS = "apply.nft_owners"  # internal: {ip: owner user id (0 = adopted profile)}


def _nft_owners(state: DesiredState) -> dict[str, int]:
    owners = {ip: 0 for ip in foreign_loopback_ips(state)}
    owners.update({u.loopback_ip: u.id for u in active_users(state)})
    return owners


def _load_nft_owners(conn: sqlite3.Connection) -> dict[str, int]:
    raw = repo.get_setting(conn, KEY_NFT_OWNERS)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): int(v) for k, v in data.items()}
    except (ValueError, TypeError, AttributeError):
        return {}


class _OperationTxn:
    """The operation transaction.

    File databases get a dedicated second connection (synchronous=FULL): other writers are
    kept out by the pipeline's write lock, and readers on the main connection never see
    uncommitted rows. An in-memory database cannot be opened twice, so the shared connection
    is used there.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._conn: sqlite3.Connection | None = None
        self._path: str | None = None
        self._path_known = False

    @property
    def path(self) -> str | None:
        """Path of the database file (None for an in-memory database)."""
        if not self._path_known:
            self._path = backup_mod.db_file_of(self._db)
            self._path_known = True
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            path = self.path
            if path:
                self._conn = connect(path)
                self._conn.execute("PRAGMA synchronous = FULL")  # durable commit of operations
            else:
                self._conn = self._db.conn
        return self._conn

    @property
    def shared(self) -> bool:
        """True when the main connection itself carries the transaction (in-memory DB)."""
        return self.conn is self._db.conn

    async def begin(self) -> None:
        conn = self.conn
        await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")

    async def commit(self) -> None:
        conn = self.conn
        await asyncio.to_thread(conn.execute, "COMMIT")

    async def rollback(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                await asyncio.to_thread(conn.execute, "ROLLBACK")

    def close(self) -> None:
        if self._conn is not None and self._conn is not self._db.conn:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
        self._conn = None


# ---------------------------------------------------------------------------- the pipeline


class ApplyPipeline:
    def __init__(
        self,
        ops: SystemOps,
        db: Database,
        config: ApplyConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_failure: Callable[[ApplyFailure], Awaitable[None]] | None = None,
    ) -> None:
        self.ops = ops
        self.db = db
        self.config = config or ApplyConfig()
        self._clock = clock or _utcnow
        self._sleep = sleep
        self.on_failure = on_failure
        self._txn = _OperationTxn(db)
        self._pending: list[_Submission] = []
        self._driver: asyncio.Task[None] | None = None
        self._db_lock = asyncio.Lock()  # held from BEGIN to COMMIT/ROLLBACK and by db_write
        self._txn_active = False
        self._applying = False
        self.current_reason: str | None = None

    # ------------------------------------------------------------------ public API

    @property
    def is_applying(self) -> bool:
        """True while a batch is being processed (for the "applying..." indicator)."""
        return self._applying or bool(self._pending)

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        self._txn.close()

    async def run_operation[T](
        self,
        mutation: Callable[[sqlite3.Connection], T],
        *,
        reason: str,
        actor: str = "system",
        force_external: bool = False,
        expect_profiles_hash: str | None = None,
        full_nft_reload: bool = False,
        bypass_adoption: bool = False,
        prune_orphans: bool = False,
    ) -> OperationOutcome[T]:
        """Perform ``mutation`` inside the operation transaction and apply the result.

        Returns after the relay is healthy (success) or after a complete rollback (failure).
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OperationOutcome[Any]] = loop.create_future()
        self._pending.append(
            _Submission(
                mutation,
                reason,
                actor,
                force_external,
                expect_profiles_hash,
                full_nft_reload,
                future,
                bypass_adoption,
                prune_orphans,
            )
        )
        if self._driver is None or self._driver.done():
            self._driver = asyncio.create_task(self._drain())
        outcome: OperationOutcome[T] = await future
        return outcome

    async def apply_now(
        self,
        reason: str = "manual",
        actor: str = "system",
        *,
        force_external: bool = False,
        full_nft_reload: bool = False,
    ) -> OperationOutcome[None]:
        """Re-render everything from the DB and reconcile the system (no DB mutation)."""
        return await self.run_operation(
            lambda conn: None,
            reason=reason,
            actor=actor,
            force_external=force_external,
            full_nft_reload=full_nft_reload,
        )

    async def db_write[R](
        self, fn: Callable[..., R], /, *args: Any, wait_s: float | None = None, **kwargs: Any
    ) -> R:
        """THE way to write to the database outside operations (collector, bot, web).

        Takes the same asyncio lock that an operation holds from BEGIN to COMMIT/ROLLBACK, so
        a writer never meets "database is locked", never waits for SQLite's busy timeout and is
        never swallowed into an operation transaction. Waits at most ``wait_s`` seconds
        (default ``ApplyTiming.db_write_timeout_s``), then raises ``DbWriteTimeout`` with a
        clear Russian message. Reads may use ``db.run`` directly.
        """
        limit = self.config.timing.db_write_timeout_s if wait_s is None else wait_s
        try:
            await asyncio.wait_for(self._db_lock.acquire(), limit)
        except TimeoutError:
            raise DbWriteTimeout(
                "База данных занята применением изменений; повторите через минуту"
            ) from None
        try:
            return await self.db.run(fn, *args, **kwargs)
        finally:
            self._db_lock.release()

    async def _txn_begin(self) -> None:
        await self._db_lock.acquire()
        try:
            await self._txn.begin()
        except BaseException:
            self._db_lock.release()
            raise
        self._txn_active = True

    async def _txn_commit(self) -> None:
        """COMMIT and release the write lock. On a COMMIT error the transaction stays open."""
        try:
            await self._txn.commit()
        except sqlite3.Error as exc:
            raise ApplyError("commit", type(exc).__name__) from None
        self._txn_active = False
        self._db_lock.release()

    async def _txn_abort(self) -> None:
        """ROLLBACK (if open) and release the write lock; safe to call repeatedly."""
        if not self._txn_active:
            return
        try:
            await self._txn.rollback()
        finally:
            self._txn_active = False
            self._db_lock.release()

    async def detect_drift(self) -> DriftReport | None:
        """Compare profiles.json with the hash we last wrote (read-only; used by doctor)."""
        report, _ = await self._drift()
        return report

    async def create_backup(
        self, reason: str = "manual", actor: str = "system", *, full: bool = True
    ) -> BackupInfo:
        """Manual / scheduled backup (FULL snapshot incl. statistics), serialised by the lock.

        Pre-apply backups taken by the pipeline itself are slim (no traffic/history tables).
        """
        try:
            lock = await self.ops.acquire_lock(
                self.config.paths.lock, self.config.timing.lock_timeout_s
            )
        except SystemOpsError as exc:
            raise BackupError(f"не удалось получить блокировку: {_clean(str(exc))}") from None
        tmp = backup_mod.make_temp_dir()
        try:
            now = self._clock()
            snap = tmp / "snapshot.db"
            try:
                await asyncio.to_thread(
                    backup_mod.snapshot_database, self.db, self._txn.path, snap, slim=not full
                )
            except (OSError, sqlite3.Error) as exc:
                raise BackupError(f"не удалось снять снимок БД: {type(exc).__name__}") from None
            info = await backup_mod.create_backup(
                self.ops,
                self.config.paths,
                reason=reason,
                now=now,
                db_file=str(snap),
                slim=not full,
            )

            def record(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    backup_mod.record_backup(conn, info)
                    repo.add_audit(conn, now, actor, "backup.create", "", info.reason)

            await self.db_write(record)
            await self._prune()
            return info
        finally:
            backup_mod.remove_temp_dir(tmp)
            with contextlib.suppress(Exception):
                await lock.release()

    async def sync_backups(self) -> int:
        """Register backups found on disk but unknown to the DB. Returns how many were added."""
        found = await backup_mod.scan_disk_backups(self.ops, self.config.paths)
        return await self.db_write(backup_mod.add_missing_backups, found)

    async def _resolve_backup(self, source: int | str, allow_external_path: bool) -> str | None:
        """Path of a restorable archive: only registered copies inside the backups directory
        (by id or by path), unless the caller explicitly allows an external file (CLI)."""
        records = await self.db.run(repo.list_backups)
        base = self.config.paths.backups_dir.rstrip("/") + "/"
        if isinstance(source, int):
            for rec in records:
                if rec.id == source and rec.path.startswith(base):
                    return rec.path
            return None
        normal = posixpath.normpath(source)
        if any(r.path == normal for r in records) and normal.startswith(base):
            return normal
        return normal if allow_external_path else None

    async def restore_backup(
        self, source: int | str, actor: str = "system", *, allow_external_path: bool = False
    ) -> OperationOutcome[None]:
        """Restore the DB snapshot of an archive and reconcile the system through the pipeline.

        ``source`` is a registered backup id or path; an arbitrary file path is accepted only
        with ``allow_external_path=True`` (explicit CLI use). Proxy files are derived from the
        DB, so restoring the snapshot and re-rendering gives the archived state. A slim
        snapshot (pre-apply backup) keeps the current statistics. A fresh pre-restore backup is
        taken by the pipeline itself; on failure everything returns to the pre-restore state.
        """
        path = await self._resolve_backup(source, allow_external_path)
        if path is None:
            return OperationOutcome(
                ok=False,
                status="rejected",
                error="Восстановить можно только зарегистрированную копию из каталога бэкапов",
            )
        try:
            archive = await backup_mod.read_archive(self.ops, path)
        except BackupError as exc:
            return OperationOutcome(ok=False, status="rejected", error=str(exc))
        if not archive.has_db:
            return OperationOutcome(
                ok=False,
                status="rejected",
                error="В архиве нет снимка базы данных: восстановление невозможно",
            )
        tmp = backup_mod.make_temp_dir()
        try:
            snapshot = tmp / "restore.db"
            try:
                found = await self.ops.extract_tar_member(path, backup_mod.DB_MEMBER, str(snapshot))
            except SystemOpsError as exc:
                return OperationOutcome(
                    ok=False, status="rejected", error=f"Не удалось прочитать архив: {exc}"
                )
            if not found:
                return OperationOutcome(
                    ok=False, status="rejected", error="В архиве нет снимка базы данных"
                )
            current_hash = await self._current_profiles_hash()
            current_names = await self._current_profile_names()
            slim = archive.slim

            def mutation(conn: sqlite3.Connection) -> None:
                backup_mod.restore_db_snapshot(conn, str(snapshot), slim=slim)
                # the file on disk is what we wrote: its managed names stay "ours" after this
                repo.set_setting(conn, KEY_ALL_NAMES, json.dumps(current_names[0]))
                repo.set_setting(conn, KEY_OUR_NAMES, json.dumps(current_names[1]))
                if current_hash is None:
                    repo.delete_setting(conn, KEY_PROFILES_HASH)
                else:
                    repo.set_setting(conn, KEY_PROFILES_HASH, current_hash)
                # every panel session issued before the restore stops being valid
                version = repo.get_setting(conn, KEY_PANEL_SESSION_VERSION, "0") or "0"
                bumped = int(version) + 1 if version.isdigit() else 1
                repo.set_setting(conn, KEY_PANEL_SESSION_VERSION, str(bumped))
                repo.add_audit(conn, self._clock(), actor, "backup.restore", "", "restore")

            return await self.run_operation(
                mutation,
                reason="restore",
                actor=actor,
                force_external=True,
                expect_profiles_hash=current_hash,
                bypass_adoption=True,
                prune_orphans=True,
            )
        finally:
            backup_mod.remove_temp_dir(tmp)

    async def recover_interrupted(self) -> list[int]:
        """Mark apply runs left 'running' by a crashed process as failed. Returns their ids."""

        def fix(conn: sqlite3.Connection) -> list[int]:
            ids = [
                int(r["id"])
                for r in conn.execute("SELECT id FROM apply_runs WHERE status = 'running'")
            ]
            for run_id in ids:
                repo.finish_apply_run(
                    conn, run_id, self._clock(), "failed", error="прервано перезапуском процесса"
                )
            return ids

        return await self.db_write(fix)

    # ------------------------------------------------------------ crash recovery (startup)

    async def startup_recovery(self) -> RecoveryReport:
        """Run once at service start, before accepting operations.

        Under the apply lock: removes our leftover temp files, and for an apply run left
        'running' by a crash restores the proxy files from that run's pre-apply backup when they
        differ from it (files only, never the DB: SQLite rolls its own transaction back),
        reloads systemd, restarts the touched pools/relay, waits for /healthz, then marks the
        run failed with an explanation and writes an audit event.
        """
        report = RecoveryReport()
        paths = self.config.paths
        try:
            lock = await self.ops.acquire_lock(paths.lock, self.config.timing.lock_timeout_s)
        except SystemOpsError as exc:
            report.messages.append(f"блокировка недоступна: {_clean(str(exc))}")
            return report
        try:
            report.cleaned_temp_files = await self._cleanup_temp_files()
            journal = await self._read_journal()
            running = await self.db.run(
                lambda c: [
                    int(r["id"])
                    for r in c.execute("SELECT id FROM apply_runs WHERE status = 'running'")
                ]
            )
            note = "прервано перезапуском процесса"
            if journal is not None and (journal.get("run_id") in running):
                try:
                    restored, detail = await self._recover_files(str(journal.get("backup", "")))
                    report.restored_files = restored
                    note = f"прервано перезапуском процесса; {detail}"
                    await self._journal_clear()
                except Exception as exc:
                    note = "прервано перезапуском процесса; восстановление не удалось: " + _clean(
                        str(exc)
                    )
                    report.messages.append(note)
            elif journal is not None:
                await self._journal_clear()  # stale: that run had finished
            if running:
                report.run_ids = running

                def mark(conn: sqlite3.Connection) -> None:
                    with transaction(conn):
                        for run_id in running:
                            repo.finish_apply_run(
                                conn, run_id, self._clock(), "failed", error=_clean(note)
                            )
                            repo.add_audit(
                                conn,
                                self._clock(),
                                "system",
                                "apply.recovered",
                                f"apply:{run_id}",
                                _clean(note),
                            )

                await self.db_write(mark)
            report.messages.append(note) if running else None
            return report
        finally:
            with contextlib.suppress(Exception):
                await lock.release()

    async def _cleanup_temp_files(self) -> int:
        """Remove our own leftovers (``.tgpanel-*`` temp/check files) in the dirs we write to."""
        paths = self.config.paths
        removed = 0
        for directory in (
            paths.tproxy_dir,
            paths.tgpanel_dir,
            paths.pools_dir,
            paths.systemd_dir,
            paths.state_dir,
        ):
            try:
                names = await self.ops.list_dir(directory)
            except SystemOpsError:
                continue
            for name in names:
                if _TEMP_NAME_RE.match(name):
                    with contextlib.suppress(SystemOpsError):
                        await self.ops.remove(f"{directory}/{name}")
                        removed += 1
        return removed

    async def _read_journal(self) -> dict[str, Any] | None:
        path = self.config.paths.journal
        try:
            if not await self.ops.exists(path):
                return None
            raw = json.loads(await self.ops.read_file(path))
        except (SystemOpsError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    async def _journal_write(self, run_id: int, backup_path: str) -> None:
        doc = json.dumps({"run_id": run_id, "backup": backup_path, "phase": "writing"}).encode()
        try:
            await self.ops.write_atomic(
                self.config.paths.journal, doc, mode=0o600, owner="root", group="root"
            )
        except SystemOpsError as exc:
            raise ApplyError(
                "backup", f"не удалось записать журнал применения: {_clean(str(exc))}"
            ) from None

    async def _journal_clear(self) -> None:
        with contextlib.suppress(SystemOpsError):
            await self.ops.remove(self.config.paths.journal)

    def _managed_member(self, name: str) -> bool:
        paths = self.config.paths
        path = "/" + name
        return path in (paths.profiles, paths.config, paths.pool_unit, paths.nft_file) or bool(
            re.fullmatch(re.escape(paths.pools_dir) + r"/[0-9]+\.env", path)
        )

    async def _recover_files(self, backup_path: str) -> tuple[list[str], str]:
        """Restore managed files that differ from the pre-apply archive. Returns (paths, note)."""
        cfg, paths, ops = self.config, self.config.paths, self.ops
        if not backup_path:
            return [], "журнал без резервной копии"
        manifest_raw = (await ops.read_tar_members(backup_path, {backup_mod.MANIFEST_NAME})).get(
            backup_mod.MANIFEST_NAME
        )
        if manifest_raw is None:
            raise BackupError("в копии нет MANIFEST.json")
        files = json.loads(manifest_raw).get("files", {})
        wanted = [n for n in files if self._managed_member(n)]
        members = await ops.read_tar_members(backup_path, set(wanted))
        restored: list[str] = []
        unit_changed = relay_changed = False
        pools_changed: set[int] = set()
        for name in wanted:
            path = "/" + name
            current: bytes | None
            try:
                current = await ops.read_file(path) if await ops.exists(path) else None
            except SystemOpsError:
                current = None
            if current == members.get(name):
                continue
            meta = files[name]
            await ops.write_atomic(
                path,
                members[name],
                mode=int(meta["mode"]),
                owner=str(meta["owner"]),
                group=str(meta["group"]),
            )
            restored.append(path)
            if path == paths.pool_unit:
                unit_changed = True
            elif path in (paths.profiles, paths.config):
                relay_changed = True
            elif path.startswith(paths.pools_dir + "/"):
                pools_changed.add(int(path.rsplit("/", 1)[1].split(".")[0]))
        # pool env files created by the interrupted run did not exist in the backup: remove them
        try:
            on_disk = await ops.list_dir(paths.pools_dir)
        except SystemOpsError:
            on_disk = []
        archived_envs = {n for n in files if n.startswith(paths.pools_dir.lstrip("/") + "/")}
        for fname in on_disk:
            m = _POOL_ENV_RE.match(fname)
            if m is None or f"{paths.pools_dir.lstrip('/')}/{fname}" in archived_envs:
                continue
            pool_id = int(m.group(1))
            with contextlib.suppress(SystemOpsError):
                await ops.systemctl("disable-now", cfg.pool_unit_name(pool_id))
            await ops.remove(f"{paths.pools_dir}/{fname}")
            restored.append(f"{paths.pools_dir}/{fname}")
        if not restored:
            return [], "файлы совпадают с копией, восстановление не потребовалось"
        if unit_changed:
            await ops.systemctl("daemon-reload", "")
        for pool_id in sorted(pools_changed):
            unit = cfg.pool_unit_name(pool_id)
            await ops.systemctl("restart" if await ops.is_active(unit) else "enable-now", unit)
        if relay_changed or pools_changed:
            await ops.systemctl("restart", cfg.relay_unit)
            if not await self._healthz():
                raise ApplyError("health", "relay не ответил на /healthz после восстановления")
        return restored, f"восстановлено файлов из копии: {len(restored)}"

    # ------------------------------------------------------------ adoption / orphans

    async def adopt(self, actor: str = "system") -> str:
        """``tgpanel apply --adopt``: record the current profiles.json as the baseline.

        Nothing is rendered or restarted. Entries named ``u<id>`` that the DB does not know
        become unmanaged pass-through entries and every orphan pool is left alone.
        """
        lock = await self.ops.acquire_lock(
            self.config.paths.lock, self.config.timing.lock_timeout_s
        )
        try:
            names = await self._current_profile_names()
            digest = await self._current_profiles_hash()

            def write(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    if digest is not None:
                        repo.set_setting(conn, KEY_PROFILES_HASH, digest)
                    repo.set_setting(conn, KEY_ALL_NAMES, json.dumps(names[0]))
                    repo.set_setting(conn, KEY_OUR_NAMES, json.dumps([]))
                    repo.set_setting(conn, KEY_ADOPTED, "1")
                    repo.add_audit(conn, self._clock(), actor, "apply.adopt", "", "")

            await self.db_write(write)
            return "Текущее состояние profiles.json принято как базовое; пулы не тронуты"
        finally:
            with contextlib.suppress(Exception):
                await lock.release()

    async def prune_orphans(self, actor: str = "system") -> OperationOutcome[None]:
        """Explicit cleanup of pools whose id is unknown to the DB (stop + remove env)."""
        return await self.run_operation(
            lambda conn: None, reason="prune-orphans", actor=actor, prune_orphans=True
        )

    async def _adoption_gap(self) -> str | None:
        """Russian refusal text when the DB is empty/new over a live install, else None."""
        paths, ops = self.config.paths, self.ops
        try:
            names = await ops.list_dir(paths.pools_dir)
        except SystemOpsError:
            names = []
        env_ids = [n for n in names if _POOL_ENV_RE.match(n)]
        try:
            entries = parse_profiles(await ops.read_file(paths.profiles))
        except (SystemOpsError, RenderError):
            entries = []
        artefacts = [
            e
            for e in entries
            if _USER_PROFILE_RE.fullmatch(e.name) or e.backend.startswith("127.64.")
        ]
        if not env_ids and not artefacts:
            return None

        def facts(conn: sqlite3.Connection) -> tuple[str | None, int, int]:
            if repo.get_setting(conn, KEY_ADOPTED):
                return "adopted", 1, 1
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pools = conn.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
            return repo.get_setting(conn, KEY_PROFILES_HASH), int(users), int(pools)

        stored, users, pools = await self.db.run(facts)
        if stored is None or (users == 0 and pools == 0 and env_ids):
            return (
                f"На сервере уже есть пулы панели ({len(env_ids)}) и профили панели "
                f"({len(artefacts)}), а база данных о них не знает (пустая или новая). "
                "Применение остановлено, чтобы не остановить работающие пулы. "
                "Восстановите базу из резервной копии (tgpanel restore <файл>) или "
                "выполните tgpanel apply --adopt."
            )
        return None

    async def set_legacy_mtproxy(
        self, enabled: bool, actor: str = "system", *, force: bool = False
    ) -> str:
        """``tgpanel legacy-mtproxy on|off``: unmask+start / mask-now the legacy unit.

        If ``mask`` is refused (the unit is a regular file in /etc/systemd/system) the fallback
        is stop + disable + a drop-in with an always-false ``ConditionPathExists``; ``on``
        removes that drop-in again. Raises OperationRejected when refused.
        """
        cfg = self.config
        unit = cfg.paths.legacy_unit
        ops = self.ops
        try:
            lock = await ops.acquire_lock(cfg.paths.lock, cfg.timing.lock_timeout_s)
        except SystemOpsError as exc:
            raise OperationRejected(f"Не удалось получить блокировку: {_clean(str(exc))}") from None
        try:
            if not enabled:
                if not force:
                    users = await self._profiles_on_legacy()
                    if users:
                        raise OperationRejected(
                            f"{users} профилей relay (в том числе не импортированные) всё ещё "
                            f"используют старый процесс MTProxy (порт {cfg.legacy_port}). "
                            "Сначала импортируйте их или используйте --force."
                        )
                # Remember the facts before the unit disappears (a masked unit is empty).
                with contextlib.suppress(RenderError, SystemOpsError):
                    facts = await read_live_facts(ops, cfg.paths)
                    await self.db_write(self._save_facts, facts_to_json(facts))
                try:
                    await ops.systemctl("mask-now", unit)
                    text = "Старый процесс MTProxy остановлен и замаскирован"
                except SystemOpsError:
                    await ops.systemctl("stop", unit)
                    await ops.systemctl("disable", unit)
                    await ops.ensure_dir(cfg.paths.legacy_dropin_dir, 0o755, "root", "root")
                    await ops.write_atomic(
                        cfg.paths.legacy_off_dropin,
                        LEGACY_OFF_DROPIN.encode(),
                        mode=0o644,
                        owner="root",
                        group="root",
                    )
                    await ops.systemctl("daemon-reload", "")
                    text = "Старый процесс MTProxy остановлен и отключён (drop-in tgpanel-off.conf)"
                action = "legacy.off"
            else:
                if await ops.exists(cfg.paths.legacy_off_dropin):
                    await ops.remove(cfg.paths.legacy_off_dropin)
                    await ops.systemctl("daemon-reload", "")
                with contextlib.suppress(SystemOpsError):
                    await ops.systemctl("unmask", unit)
                await ops.systemctl("start", unit)
                text = "Старый процесс MTProxy включён"
                action = "legacy.on"
            await self.db_write(self._audit_simple, actor, action)
            return text
        except SystemOpsError as exc:
            raise OperationRejected(f"Не удалось выполнить действие: {_clean(str(exc))}") from None
        finally:
            with contextlib.suppress(Exception):
                await lock.release()

    # ------------------------------------------------------------------ small DB helpers

    @staticmethod
    def _save_facts(conn: sqlite3.Connection, encoded: str) -> None:
        repo.set_setting(conn, KEY_MTPROXY_FACTS, encoded)

    def _audit_simple(self, conn: sqlite3.Connection, actor: str, action: str) -> None:
        repo.add_audit(conn, self._clock(), actor, action)

    async def _profiles_on_legacy(self) -> int:
        try:
            data = await self.ops.read_file(self.config.paths.profiles)
            entries = parse_profiles(data)
        except (SystemOpsError, RenderError):
            return 0
        port = f":{self.config.legacy_port}"
        return sum(1 for e in entries if e.backend.endswith(port) and e.name != SENTINEL_NAME)

    async def _current_profile_names(self) -> tuple[list[str], list[str]]:
        """(all names, names matching our own u<id>/sentinel pattern) of the current file."""
        try:
            names = [
                e.name for e in parse_profiles(await self.ops.read_file(self.config.paths.profiles))
            ]
        except (SystemOpsError, RenderError):
            return [], []
        ours = [n for n in names if n == SENTINEL_NAME or _USER_PROFILE_RE.fullmatch(n)]
        return names, ours

    async def _current_profiles_hash(self) -> str | None:
        try:
            return profiles_hash(await self.ops.read_file(self.config.paths.profiles))
        except (SystemOpsError, RenderError):
            return None

    # ------------------------------------------------------------------ queue / driver

    async def _drain(self) -> None:
        while self._pending:
            batch, self._pending = self._pending, []
            self._applying = True
            try:
                await self._process_batch(batch)
            except BaseException as exc:
                log.exception("apply batch crashed")
                for sub in batch:
                    if not sub.future.done():
                        sub.future.set_result(
                            OperationOutcome(
                                ok=False,
                                status="failed",
                                error="Внутренняя ошибка применения изменений",
                            )
                        )
                if not isinstance(exc, Exception):
                    raise
            finally:
                self._applying = False
                self.current_reason = None
        self._driver = None

    async def _process_batch(self, batch: list[_Submission]) -> None:
        subs = [s for s in batch if not s.future.done()]
        if not subs:
            return
        self.current_reason = "; ".join(dict.fromkeys(s.reason for s in subs))[:200]
        try:
            lock = await self.ops.acquire_lock(
                self.config.paths.lock, self.config.timing.lock_timeout_s
            )
        except SystemOpsError as exc:
            text = f"Не удалось получить блокировку применения: {_clean(str(exc))}"
            for sub in subs:
                self._resolve(sub, OperationOutcome(ok=False, status="failed", error=text))
            return
        try:
            retry = await self._locked_batch(subs)
            # Poison-operation guard: a failed batch is retried once, one operation per apply,
            # so a single bad request cannot fail unrelated ones.
            for sub in retry:
                if not sub.future.done():
                    self.current_reason = sub.reason[:200]
                    await self._locked_batch([sub], retry_note=BATCH_FAILED_NOTE)
        finally:
            with contextlib.suppress(Exception):
                await lock.release()

    @staticmethod
    def _resolve(sub: _Submission, outcome: OperationOutcome[Any]) -> None:
        if not sub.future.done():
            sub.future.set_result(outcome)

    # ------------------------------------------------------------------ drift

    async def _drift(self) -> tuple[DriftReport | None, str | None]:
        """(report, current hash of profiles.json)."""
        paths = self.config.paths
        try:
            data = await self.ops.read_file(paths.profiles)
        except SystemOpsError:
            return None, None  # missing file: we simply recreate it
        try:
            current = profiles_hash(data)
        except RenderError:
            return DriftReport("profiles.json повреждён (не является корректным JSON)"), None
        stored = await self.db.run(repo.get_setting, KEY_PROFILES_HASH)
        if stored == current:
            return None, current
        try:
            names = [e.name for e in parse_profiles(data)]
        except RenderError:
            return DriftReport("profiles.json имеет неожиданную структуру"), current
        if stored is None:
            return None, current  # first apply: unmanaged profiles are passed through
        expected = await self.db.run(self._expected_names)
        added = sorted(set(names) - expected)
        missing = sorted(expected - set(names))
        parts = []
        if added:
            parts.append(f"добавлены вне панели: {self._names(added)}")
        if missing:
            parts.append(f"отсутствуют: {self._names(missing)}")
        if not parts:
            parts.append("изменены параметры профилей (секреты, адреса или режимы)")
        return DriftReport("; ".join(parts)), current

    @staticmethod
    def _names(names: list[str], limit: int = 10) -> str:
        shown = ", ".join(names[:limit])
        return shown + (f" и ещё {len(names) - limit}" if len(names) > limit else "")

    @staticmethod
    def _expected_names(conn: sqlite3.Connection) -> set[str]:
        written = stored_names(conn, KEY_ALL_NAMES)
        if written:
            return written
        users = repo.all_users(conn)
        active = {u.profile_name for u in users if u.status is UserStatus.ACTIVE}
        return active or {SENTINEL_NAME}

    # ------------------------------------------------------------------ the batch

    async def _locked_batch(
        self, subs: list[_Submission], *, retry_note: str | None = None
    ) -> list[_Submission]:
        """Process one batch under the flock. Returns submissions to retry individually."""
        started = self._clock()
        reason = "; ".join(dict.fromkeys(s.reason for s in subs))[:200] or "apply"
        run_id: int = await self.db.run(repo.start_apply_run, started, reason)
        journal = _Journal()
        plan: _Plan | None = None
        backup: BackupInfo | None = None
        outcomes: dict[int, OperationOutcome[Any]] = {}  # index in subs -> outcome
        values: dict[int, Any] = {}
        survivors: list[int] = []
        snap_dir: Path | None = None

        async def finish(
            status: str,
            error: str | None,
            *,
            keep_row: bool = True,
            audit: tuple[str, str] | None = None,
        ) -> None:
            await self._finalize(run_id, started, status, error, backup, keep_row, audit, subs)

        try:
            # ---- adoption / drift / expectations --------------------------------------
            gap = await self._adoption_gap() if any(not s.bypass_adoption for s in subs) else None
            report, current_hash = await self._drift()
            runnable: list[int] = []
            for i, sub in enumerate(subs):
                if gap is not None and not sub.bypass_adoption:
                    outcomes[i] = OperationOutcome(
                        ok=False, status="needs_adoption", error=gap, apply_run_id=run_id
                    )
                elif report is not None and not sub.force_external:
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="external_change",
                        error=(
                            "profiles.json изменён вне панели: "
                            f"{report.description}. Операция не выполнена."
                        ),
                        apply_run_id=run_id,
                        external=report.exception(),
                    )
                elif sub.expect_hash is not None and sub.expect_hash != current_hash:
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="rejected",
                        error="profiles.json изменился во время подготовки операции; повторите",
                    )
                else:
                    runnable.append(i)
            if gap is not None and not runnable:
                await finish("failed", _clean(gap), audit=("apply.needs_adoption", _clean(gap)))
                self._publish(subs, outcomes)
                return []
            if report is not None and not runnable:
                await finish(
                    "failed",
                    _clean(f"внешнее изменение profiles.json: {report.description}"),
                    audit=("apply.external_change", _clean(report.description)),
                )
                self._publish(subs, outcomes)
                return []
            if not runnable:
                await finish("rejected", None, keep_row=False)
                self._publish(subs, outcomes)
                return []

            # ---- DB snapshot BEFORE the write transaction (separate read connection) ---
            snap_dir = backup_mod.make_temp_dir()
            snap_file = snap_dir / "snapshot.db"
            try:
                await asyncio.to_thread(
                    backup_mod.snapshot_database, self.db, self._txn.path, snap_file, slim=True
                )
            except (OSError, sqlite3.Error) as exc:
                raise ApplyError("backup", f"снимок БД: {type(exc).__name__}") from None

            # ---- transaction + mutations ------------------------------------------------
            await self._txn_begin()
            conn = self._txn.conn
            for i in runnable:
                try:
                    values[i] = self._run_mutation(conn, subs[i], i)
                    survivors.append(i)
                except OperationRejected as exc:
                    outcomes[i] = OperationOutcome(ok=False, status="rejected", error=str(exc))
                except sqlite3.IntegrityError:
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="rejected",
                        error="Нарушено ограничение уникальности (имя, Telegram ID или секрет)",
                    )
                except (BackupError, SettingsError) as exc:
                    outcomes[i] = OperationOutcome(ok=False, status="rejected", error=str(exc))
                except Exception as exc:
                    log.warning("mutation failed: %s", type(exc).__name__)
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="failed",
                        error=f"Внутренняя ошибка операции ({type(exc).__name__})",
                    )
            if not survivors:
                await self._txn_abort()
                await finish("rejected", None, keep_row=False)
                self._publish(subs, outcomes)
                return []

            # ---- plan, backup, validate, execute --------------------------------------
            force_nft = any(subs[i].full_nft_reload for i in survivors)
            prune = any(subs[i].prune_orphans for i in survivors)
            plan = await self._build_plan(conn, force_nft, prune)
            if plan.noop:
                self._store_hash(conn, plan)
                await self._txn_commit()
                notes: list[str] = []
                await self._cleanup_stale(plan, notes)
                await finish("noop", None, keep_row=False)
                for i in survivors:
                    outcomes[i] = OperationOutcome(
                        ok=True,
                        status="noop",
                        value=values[i],
                        warnings=tuple([*plan.warnings, *notes]),
                    )
                self._publish(subs, outcomes)
                return []

            backup = await self._backup(plan, reason, snap_file)
            await self._journal_write(run_id, backup.path)
            await self._validate(plan)
            await self._execute(plan, journal)
            self._store_hash(conn, plan)
            await self._txn_commit()
        except BaseException as exc:
            retry = await self._fail(
                exc, subs, outcomes, run_id, plan, journal, finish, survivors, retry_note
            )
            if not isinstance(exc, Exception):
                raise
            return retry
        finally:
            await self._txn_abort()
            backup_mod.remove_temp_dir(snap_dir)

        # ---- success: everything below happens AFTER the commit and must never turn the
        # ---- operation into a failure (it is durable); problems become warnings. --------
        if plan is None:  # pragma: no cover - unreachable, keeps mypy honest
            raise RuntimeError("plan missing after a successful apply")
        warnings = list(plan.warnings)
        if retry_note is not None:
            warnings.append("Групповое применение не удалось; операция выполнена отдельно")
        await self._after_commit(plan, finish, warnings)
        for i in survivors:
            outcomes[i] = OperationOutcome(
                ok=True,
                status="applied",
                value=values[i],
                apply_run_id=run_id,
                warnings=tuple(warnings),
            )
        self._publish(subs, outcomes)
        return []

    async def _after_commit(
        self, plan: _Plan, finish: Callable[..., Awaitable[None]], warnings: list[str]
    ) -> None:
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("очистка устаревших файлов", lambda: self._cleanup_stale(plan, warnings)),
            ("запись результата применения", lambda: finish("success", None)),
            ("удаление журнала применения", self._journal_clear),
            ("очистка старых бэкапов", self._prune),
        ]
        for label, step in steps:
            try:
                await step()
            except Exception as exc:
                log.warning("post-commit step failed: %s (%s)", label, type(exc).__name__)
                warnings.append(f"После применения не удалось: {label}")

    def _publish(self, subs: list[_Submission], outcomes: dict[int, OperationOutcome[Any]]) -> None:
        for i, sub in enumerate(subs):
            self._resolve(
                sub,
                outcomes.get(
                    i, OperationOutcome(ok=False, status="failed", error="Операция не выполнена")
                ),
            )

    def _run_mutation(self, conn: sqlite3.Connection, sub: _Submission, index: int) -> Any:
        name = f"op_{index}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            value = sub.mutation(conn)
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return value

    def _store_hash(self, conn: sqlite3.Connection, plan: _Plan) -> None:
        repo.set_setting(conn, KEY_PROFILES_HASH, profiles_hash(plan.rendered.profiles_json))
        repo.set_setting(conn, KEY_NFT_OWNERS, json.dumps(_nft_owners(plan.state)))
        names = [e.name for e in parse_profiles(plan.rendered.profiles_json)]
        foreign_names = {json.loads(f).get("name") for f in plan.state.foreign_profiles}
        repo.set_setting(conn, KEY_ALL_NAMES, json.dumps(names))
        repo.set_setting(
            conn, KEY_OUR_NAMES, json.dumps([n for n in names if n not in foreign_names])
        )

    # ------------------------------------------------------------------ failure handling

    async def _fail(
        self,
        exc: BaseException,
        subs: list[_Submission],
        outcomes: dict[int, OperationOutcome[Any]],
        run_id: int,
        plan: _Plan | None,
        journal: _Journal,
        finish: Callable[..., Awaitable[None]],
        survivors: list[int],
        retry_note: str | None,
    ) -> list[_Submission]:
        if isinstance(exc, ApplyError):
            stage, detail = exc.stage, _clean(exc.detail)
        elif isinstance(exc, SystemOpsError):
            stage, detail = "internal", _clean(str(exc))
        elif isinstance(exc, (RenderError, BackupError)):
            stage, detail = "render", _clean(str(exc))
        elif isinstance(exc, asyncio.CancelledError):
            stage, detail = "internal", "применение прервано"
        else:
            log.warning("apply failed: %s", type(exc).__name__, exc_info=True)
            stage, detail = "internal", type(exc).__name__
        rollback_errors = await asyncio.shield(self._rollback(plan, journal))
        await self._txn_abort()
        text = (
            f"Не удалось применить изменения ({STAGE_RU.get(stage, stage)}): {detail}. "
            "Операция не выполнена, изменения отменены."
        )
        if retry_note:
            text = retry_note + text
        if rollback_errors:
            text += " ВНИМАНИЕ: откат выполнен не полностью, проверьте состояние сервисов."
        tech = f"[{stage}] {detail}"
        if rollback_errors:
            tech += " | rollback: " + "; ".join(rollback_errors)
        await finish("failed", _clean(tech), audit=("apply.failed", _clean(tech)))
        if not rollback_errors:
            await self._journal_clear()  # a dirty rollback keeps it for startup_recovery
        retryable = (
            isinstance(exc, Exception)
            and retry_note is None
            and len(survivors) > 1
            and not rollback_errors
        )
        retry: list[_Submission] = []
        for i in range(len(subs)):
            if i in outcomes:  # rejected / external_change / own failure: keep as is
                continue
            if retryable and i in survivors:
                retry.append(subs[i])
                continue
            outcomes[i] = OperationOutcome(
                ok=False,
                status="failed",
                error=text,
                apply_run_id=run_id,
                rolled_back=True,
                rollback_errors=tuple(rollback_errors),
            )
        for i, sub in enumerate(subs):
            if i in outcomes:
                self._resolve(sub, outcomes[i])
        if self.on_failure is not None and not retryable:
            with contextlib.suppress(Exception):
                await self.on_failure(
                    ApplyFailure(
                        run_id,
                        self.current_reason or "",
                        subs[0].actor if subs else "system",
                        text,
                        True,
                        tuple(rollback_errors),
                    )
                )
        return retry

    async def _finalize(
        self,
        run_id: int,
        started: datetime,
        status: str,
        error: str | None,
        backup: BackupInfo | None,
        keep_row: bool,
        audit: tuple[str, str] | None,
        subs: list[_Submission],
    ) -> None:
        now = self._clock()
        actor = subs[0].actor if subs else "system"

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                if backup is not None:
                    backup_mod.record_backup(conn, backup)
                if keep_row:
                    repo.finish_apply_run(
                        conn,
                        run_id,
                        now,
                        "success" if status == "success" else "failed",
                        error=error,
                        backup_path=None if backup is None else backup.path,
                    )
                else:
                    conn.execute("DELETE FROM apply_runs WHERE id = ?", (run_id,))
                if audit is not None:
                    repo.add_audit(conn, now, actor, audit[0], f"apply:{run_id}", audit[1])

        try:
            await self.db.run(write)
        except sqlite3.Error:
            log.error("could not record apply run %s", run_id)
        _ = started

    async def _prune(self) -> None:
        def run(conn: sqlite3.Connection) -> tuple[int, int]:
            cfg = read_settings(conn)
            return cfg.backup_keep_last, cfg.backup_keep_days

        try:
            keep_last, keep_days = await self.db.run(run)
            records = await self.db.run(repo.list_backups)
        except Exception:
            return
        doomed = backup_mod.select_prunable(records, self._clock(), keep_last, keep_days)
        for rec in doomed:
            try:
                await self.ops.remove(rec.path)
            except SystemOpsError:
                continue
            await self.db.run(repo.delete_backup, rec.id)

    # ------------------------------------------------------------------ planning

    async def _snap(self, path: str) -> _Snap:
        try:
            if not await self.ops.exists(path):
                return _Snap(None, None)
            stat = await self.ops.stat(path)
            data = await self.ops.read_file(path)
        except SystemOpsError as exc:
            raise ApplyError("render", f"не удалось прочитать файл: {_clean(str(exc))}") from None
        return _Snap(data, stat)

    async def _build_plan(
        self, conn: sqlite3.Connection, force_nft: bool, prune_orphans: bool = False
    ) -> _Plan:
        cfg, paths, ops = self.config, self.config.paths, self.ops
        now = self._clock()
        try:
            settings = read_settings(conn)
            profiles_snap = await self._snap(paths.profiles)
            foreign = split_foreign(
                profiles_snap.data, repo.all_users(conn), stored_names(conn, KEY_OUR_NAMES)
            )
            state = load_desired_state(conn, now, settings, foreign)
            errors = validate_desired_state(state)
            if errors:
                raise ApplyError("validate", "; ".join(errors[:5]))
            facts, warnings = await resolve_facts(ops, paths, conn)
            warnings = list(warnings)
            config_snap = await self._snap(paths.config)
            if config_snap.data is None:
                raise ApplyError("render", "config.json relay не найден")
            rendered = render_all(state, config_snap.data, facts)
        except (RenderError, SettingsError) as exc:
            raise ApplyError("render", _clean(str(exc))) from None
        warnings += capacity_warnings(state)

        def keep_or(snap: _Snap, default: tuple[int, str, str]) -> tuple[int, str, str]:
            if snap.stat is None:
                return default
            return snap.stat.mode, snap.stat.owner, snap.stat.group

        snaps: dict[str, _Snap] = {paths.config: config_snap}
        snaps[paths.profiles] = profiles_snap
        targets: dict[str, _Target] = {}
        # profiles.json is ALWAYS 0400 (tproxy-server refuses files readable or writable by
        # group/others, even for -check); owner/group of an existing file are kept.
        pm, po, pg = keep_or(snaps[paths.profiles], (PROFILES_MODE, "root", cfg.tproxy_group))
        if snaps[paths.profiles].stat is not None and pm & 0o077:
            warnings.append(
                f"profiles.json имел права {pm:04o}: исправлены на {PROFILES_MODE:04o} "
                "(tproxy-server отклоняет файлы, доступные группе или всем)"
            )
        targets[paths.profiles] = _Target(
            paths.profiles, rendered.profiles_json, PROFILES_MODE, po, pg
        )
        cm, co, cg = keep_or(config_snap, (0o640, "root", cfg.tproxy_group))
        targets[paths.config] = _Target(paths.config, rendered.config_json, cm, co, cg)
        targets[paths.pool_unit] = _Target(
            paths.pool_unit, rendered.pool_unit, 0o644, "root", "root"
        )
        for pool_id, env in rendered.pool_envs.items():
            env_path = paths.pool_env(pool_id)
            targets[env_path] = _Target(env_path, env, 0o600, "root", "root")
        targets[paths.nft_file] = _Target(paths.nft_file, rendered.nft_file, 0o600, "root", "root")
        for path in targets:
            if path not in snaps:
                snaps[path] = await self._snap(path)
        changed = {path for path, t in targets.items() if self._differs(snaps[path], t)}

        pools_by_id = {p.id: p for p in state.pools}
        unit_changed = paths.pool_unit in changed
        to_start: list[tuple[PoolRecord, bool]] = []
        try:
            for pool_id in rendered.pools_to_run:
                active = await ops.is_active(cfg.pool_unit_name(pool_id))
                if paths.pool_env(pool_id) in changed or unit_changed or not active:
                    to_start.append((pools_by_id[pool_id], active))
            to_stop: list[int] = []
            stale: list[str] = []
            # Only pools that EXIST in the DB and are empty are stopped here.
            for pool_id in rendered.pools_to_stop:
                unit = cfg.pool_unit_name(pool_id)
                if await ops.is_active(unit) or await self._unit_enabled(unit):
                    to_stop.append(pool_id)
                env_path = paths.pool_env(pool_id)
                if await ops.exists(env_path):
                    stale.append(env_path)
            # Pools unknown to the DB are never touched, except on explicit prune / restore.
            try:
                names = await ops.list_dir(paths.pools_dir)
            except SystemOpsError:
                names = []
            for name in names:
                m = _POOL_ENV_RE.match(name)
                if m is None or int(m.group(1)) in pools_by_id:
                    continue
                orphan = int(m.group(1))
                if not prune_orphans:
                    warnings.append(f"Пул {orphan} неизвестен базе данных и оставлен без изменений")
                    continue
                unit = cfg.pool_unit_name(orphan)
                if await ops.is_active(unit) or await self._unit_enabled(unit):
                    to_stop.append(orphan)
                stale.append(f"{paths.pools_dir}/{name}")

            keep_ips = {u.loopback_ip for u in active_users(state)}
            keep_ips |= set(foreign_loopback_ips(state))  # accounting of adopted profiles stays
            desired_ips = sorted(keep_ips)
            nft_changes: list[tuple[str, list[str], list[str]]] = []
            nft_recreate: list[str] = []
            table_existed = True
            try:
                for set_name in ("up", "down"):
                    current = set(await ops.nft_list_set("tgpanel", set_name))
                    want = set(desired_ips)
                    nft_changes.append((set_name, sorted(want - current), sorted(current - want)))
                    if set_name == "up":
                        old_owners = _load_nft_owners(conn)
                        new_owners = _nft_owners(state)
                        nft_recreate = sorted(
                            ip
                            for ip in want & current
                            if ip in old_owners and old_owners[ip] != new_owners.get(ip)
                        )
            except NftTableMissing:
                table_existed = False  # the table is missing: the whole file must be loaded
                nft_changes = []
                nft_recreate = []
            nft_full = force_nft or not table_existed
            if nft_full:
                nft_changes = []
                nft_recreate = []
            relay_restart = bool({paths.profiles, paths.config} & changed) or not (
                await ops.is_active(cfg.relay_unit)
            )
        except SystemOpsError as exc:
            raise ApplyError("render", f"не удалось опросить систему: {_clean(str(exc))}") from None

        return _Plan(
            state=state,
            rendered=rendered,
            targets=targets,
            snaps=snaps,
            changed=changed,
            pools_by_id=pools_by_id,
            to_start=to_start,
            to_stop=to_stop,
            stale_envs=stale,
            nft_full=nft_full,
            nft_changes=nft_changes,
            relay_restart=relay_restart,
            warnings=warnings,
            nft_table_existed=table_existed,
            nft_recreate=nft_recreate,
        )

    async def _unit_enabled(self, unit: str) -> bool:
        try:
            return (await self.ops.unit_property(unit, "UnitFileState")) == "enabled"
        except SystemOpsError:
            return False

    @staticmethod
    def _differs(snap: _Snap, target: _Target) -> bool:
        if snap.data != target.data or snap.stat is None:
            return True
        st = snap.stat
        return (st.mode, st.owner, st.group) != (target.mode, target.owner, target.group)

    async def _backup(self, plan: _Plan, reason: str, snapshot: Path) -> BackupInfo:
        """Pre-apply backup: slim DB snapshot (taken before BEGIN), streamed into the archive;
        file contents come from the pre-images already read under the lock (not re-read)."""
        known = {
            path: (snap.data, snap.stat)
            for path, snap in plan.snaps.items()
            if snap.data is not None and snap.stat is not None
        }
        try:
            return await backup_mod.create_backup(
                self.ops,
                self.config.paths,
                reason=reason,
                now=self._clock(),
                db_file=str(snapshot),
                slim=True,
                known=known,
            )
        except (BackupError, OSError, sqlite3.Error) as exc:
            raise ApplyError("backup", _clean(str(exc) or type(exc).__name__)) from None

    async def _validate(self, plan: _Plan) -> None:
        """Check the new files on temp copies next to the targets, before anything is touched."""
        paths = self.config.paths
        if paths.profiles in plan.changed or paths.config in plan.changed:
            await self._check_relay_files(plan)
        if paths.nft_file in plan.changed or plan.nft_full:
            await self._check_nft_file(plan)

    async def _check_relay_files(self, plan: _Plan) -> None:
        paths, ops = self.config.paths, self.ops
        cfg_t, prof_t = plan.targets[paths.config], plan.targets[paths.profiles]
        try:
            await ops.write_atomic(
                paths.check_config,
                cfg_t.data,
                mode=cfg_t.mode,
                owner=cfg_t.owner,
                group=cfg_t.group,
            )
            # the copy is ALWAYS 0600: tproxy-server -check refuses group/other access
            await ops.write_atomic(
                paths.check_profiles,
                prof_t.data,
                mode=CHECK_COPY_MODE,
                owner=prof_t.owner,
                group=prof_t.group,
            )
            result = await ops.tproxy_check(paths.check_config, paths.check_profiles)
        except SystemOpsError as exc:
            raise ApplyError("validate", _clean(str(exc))) from None
        finally:
            for tmp in (paths.check_config, paths.check_profiles):
                with contextlib.suppress(Exception):
                    await ops.remove(tmp)
        if not result.ok:
            message = _clean(result.output)
            hint = ""
            if re.search(r"pending|budget|reserve", message, re.IGNORECASE):
                hint = " Подсказка: уменьшите лимит сессий (max_sessions_global)."
            raise ApplyError(
                "validate", f"relay (tproxy-server -check) отклонил конфигурацию: {message}.{hint}"
            )

    async def _check_nft_file(self, plan: _Plan) -> None:
        paths, ops = self.config.paths, self.ops
        target = plan.targets[paths.nft_file]
        try:
            await ops.write_atomic(
                paths.check_nft, target.data, mode=0o600, owner="root", group="root"
            )
            result = await ops.nft_check_file(paths.check_nft)
        except SystemOpsError as exc:
            raise ApplyError("validate", _clean(str(exc))) from None
        finally:
            with contextlib.suppress(Exception):
                await ops.remove(paths.check_nft)
        if not result.ok:
            raise ApplyError("validate", f"nft отклонил файл правил: {_clean(result.output)}")

    # ------------------------------------------------------------------ execution

    async def _write(self, plan: _Plan, path: str, journal: _Journal) -> None:
        t = plan.targets[path]
        journal.written.append(path)  # recorded first: a half-failed write is still restored
        try:
            await self.ops.write_atomic(path, t.data, mode=t.mode, owner=t.owner, group=t.group)
        except SystemOpsError as exc:
            raise ApplyError("write", _clean(str(exc))) from None

    async def _assert_unchanged(self, plan: _Plan, path: str) -> None:
        """TOCTOU guard: the file must still equal the pre-image captured under the lock."""
        snap = plan.snaps[path]
        try:
            current = await self.ops.read_file(path) if await self.ops.exists(path) else None
        except SystemOpsError as exc:
            raise ApplyError("write", _clean(str(exc))) from None
        if current != snap.data:
            raise ApplyError(
                "write",
                f"{posixpath.basename(path)} изменён вне панели во время применения; "
                "повторите операцию",
            )

    async def _systemctl(self, stage: str, action: str, unit: str) -> None:
        try:
            await self.ops.systemctl(action, unit)
        except SystemOpsError as exc:
            raise ApplyError(stage, _clean(str(exc))) from None

    async def _execute(self, plan: _Plan, journal: _Journal) -> None:
        cfg, paths, ops, timing = self.config, self.config.paths, self.ops, self.config.timing

        # 0. the firewall first: if the table is missing (or a reload is requested) the whole
        #    file is loaded BEFORE any pool is started, so pool ports are never exposed
        if plan.nft_full:
            await self._write(plan, paths.nft_file, journal)
            journal.nft_loaded = True  # recorded first: a half-done load must be undone too
            try:
                await ops.nft_load_file(paths.nft_file)
            except SystemOpsError as exc:
                raise ApplyError("nft", _clean(str(exc))) from None

        # A. pool unit and env files
        if paths.pool_unit in plan.changed:
            await self._write(plan, paths.pool_unit, journal)
            journal.unit_written = True
            await self._systemctl("write", "daemon-reload", "")
        for pool_id in plan.rendered.pools_to_run:
            env_path = paths.pool_env(pool_id)
            if env_path in plan.changed:
                await self._write(plan, env_path, journal)

        # B. pools first: the port must be open before the relay learns about it
        for pool, was_active in plan.to_start:
            unit = cfg.pool_unit_name(pool.id)
            journal.pools[pool.id] = was_active
            await self._systemctl("pool", "restart" if was_active else "enable-now", unit)
            try:
                opened = await ops.wait_tcp_open("127.0.0.1", pool.port, timing.port_timeout_s)
            except SystemOpsError as exc:
                raise ApplyError("pool", _clean(str(exc))) from None
            if not opened:
                raise ApplyError("pool", f"порт {pool.port} пула {pool.id} не открылся")

        # C. nft: element differences only (the whole file was handled in step 0)
        if not plan.nft_full and paths.nft_file in plan.changed:
            await self._write(plan, paths.nft_file, journal)
        try:
            if not plan.nft_full:
                if plan.nft_recreate:
                    for set_name in ("up", "down"):
                        await ops.nft_delete_elements("tgpanel", set_name, plan.nft_recreate)
                        journal.nft_deleted.append((set_name, plan.nft_recreate))
                        await ops.nft_add_elements("tgpanel", set_name, plan.nft_recreate)
                        journal.nft_added.append((set_name, plan.nft_recreate))
                for set_name, add, delete in plan.nft_changes:
                    if add:
                        await ops.nft_add_elements("tgpanel", set_name, add)
                        journal.nft_added.append((set_name, add))
                    if delete:
                        await ops.nft_delete_elements("tgpanel", set_name, delete)
                        journal.nft_deleted.append((set_name, delete))
        except SystemOpsError as exc:
            raise ApplyError("nft", _clean(str(exc))) from None

        # D. relay files (after pools), then the relay. Right before each write the file is
        #    re-read: an external change since the pre-image was taken aborts the apply.
        if paths.config in plan.changed:
            await self._assert_unchanged(plan, paths.config)
            await self._write(plan, paths.config, journal)
        if paths.profiles in plan.changed:
            await self._assert_unchanged(plan, paths.profiles)
            await self._write(plan, paths.profiles, journal)
        if plan.relay_restart:
            journal.relay_touched = True
            await self._systemctl("relay", "restart", cfg.relay_unit)

        # E. health: /healthz until up, then ONE /readyz check (few spaced retries)
        if plan.relay_restart or plan.to_start:
            await self._health()

        # F. emptied pools stop only after the relay is healthy
        for pool_id in plan.to_stop:
            journal.stopped.append(pool_id)
            await self._systemctl("pool", "disable-now", cfg.pool_unit_name(pool_id))

    async def _healthz(self) -> bool:
        t, url = self.config.timing, f"{self.config.admin_url}/healthz"
        for attempt in range(t.healthz_attempts):
            try:
                res = await self.ops.http_get(url, t.http_timeout_s)
            except SystemOpsError:
                res = None
            if res is not None and res.status == 200:
                return True
            if attempt < t.healthz_attempts - 1:
                await self._sleep(t.healthz_interval_s)
        return False

    async def _health(self) -> None:
        if not await self._healthz():
            raise ApplyError("health", "relay не ответил на /healthz")
        t, url = self.config.timing, f"{self.config.admin_url}/readyz"
        for attempt in range(t.readyz_attempts):
            try:
                res = await self.ops.http_get(url, t.http_timeout_s)
            except SystemOpsError:
                res = None
            if res is not None and res.status == 200:
                return
            if attempt < t.readyz_attempts - 1:
                await self._sleep(t.readyz_interval_s)
        raise ApplyError("health", "relay не готов (/readyz): backend недоступен")

    async def _cleanup_stale(self, plan: _Plan, warnings: list[str]) -> None:
        for path in plan.stale_envs:
            try:
                await self.ops.remove(path)
            except SystemOpsError:
                log.warning("could not remove stale pool env file")
                warnings.append("Не удалось удалить файл остановленного пула")

    # ------------------------------------------------------------------ rollback

    async def _restore_one(
        self,
        plan: _Plan,
        path: str,
        step: Callable[[str, Awaitable[Any]], Awaitable[Any]],
        errors: list[str],
    ) -> None:
        """Restore a file WE wrote to its pre-image, unless somebody else changed it since."""
        ops = self.ops
        snap, target = plan.snaps[path], plan.targets[path]
        try:
            current = await ops.read_file(path) if await ops.exists(path) else None
        except SystemOpsError:
            current = target.data  # unreadable: attempt the restore
        if current == snap.data:
            return  # nothing to undo
        if current is not None and current != target.data:
            errors.append(
                f"{posixpath.basename(path)} изменён вне панели, откат его не перезаписал"
            )
            return
        if snap.data is None or snap.stat is None:
            await step(f"remove {path}", ops.remove(path))
        else:
            await step(
                f"restore {path}",
                ops.write_atomic(
                    path,
                    snap.data,
                    mode=snap.stat.mode,
                    owner=snap.stat.owner,
                    group=snap.stat.group,
                ),
            )

    async def _rollback(self, plan: _Plan | None, journal: _Journal) -> list[str]:
        errors: list[str] = []
        cfg, ops, timing = self.config, self.ops, self.config.timing

        async def step(label: str, coro: Awaitable[Any], *, tolerate_missing: bool = False) -> Any:
            try:
                return await coro
            except Exception as exc:
                if tolerate_missing and "No such file or directory" in str(exc):
                    return None  # already gone: exactly what the rollback wants
                errors.append(f"{label}: {_clean(str(exc) or type(exc).__name__)}")
                return None

        if plan is not None:
            for path in reversed(journal.written):
                await self._restore_one(plan, path, step, errors)
            if journal.unit_written:
                await step("daemon-reload", ops.systemctl("daemon-reload", ""))
            if journal.nft_loaded:
                nft_snap = plan.snaps[self.config.paths.nft_file]
                if plan.nft_table_existed and nft_snap.data is not None:
                    await step("nft reload", ops.nft_load_file(self.config.paths.nft_file))
                else:
                    await step("nft delete table", ops.nft_delete_table("tgpanel"))
        for set_name, ips in reversed(journal.nft_added):
            await step(
                f"nft delete {set_name}",
                ops.nft_delete_elements("tgpanel", set_name, ips),
                tolerate_missing=True,
            )
        for set_name, ips in reversed(journal.nft_deleted):
            await step(f"nft add {set_name}", ops.nft_add_elements("tgpanel", set_name, ips))
        for pool_id, was_active in journal.pools.items():
            unit = cfg.pool_unit_name(pool_id)
            if was_active:
                await step(f"restart pool {pool_id}", ops.systemctl("restart", unit))
                if plan is not None:
                    port = plan.pools_by_id[pool_id].port
                    await step(
                        f"wait pool {pool_id}",
                        ops.wait_tcp_open("127.0.0.1", port, timing.port_timeout_s),
                    )
            else:
                await step(f"stop pool {pool_id}", ops.systemctl("disable-now", unit))
        for pool_id in journal.stopped:
            await step(
                f"start pool {pool_id}", ops.systemctl("enable-now", cfg.pool_unit_name(pool_id))
            )
        if journal.relay_touched:
            await step("restart relay", ops.systemctl("restart", cfg.relay_unit))
            healthy = await step("healthz", self._healthz())
            if not healthy:
                errors.append("relay не ответил на /healthz после отката")
        return errors
