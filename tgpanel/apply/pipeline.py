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
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tgpanel.apply import backup as backup_mod
from tgpanel.apply.backup import BackupError, BackupInfo
from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.errors import (
    ApplyError,
    ExternalChangeDetected,
    OperationRejected,
    SettingsError,
)
from tgpanel.apply.settings_spec import (
    KEY_ALL_NAMES,
    KEY_MTPROXY_FACTS,
    KEY_OUR_NAMES,
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
from tgpanel.render.profiles import SENTINEL_NAME, active_users, parse_profiles, profiles_hash
from tgpanel.system.ops import FileStat, SystemOps, SystemOpsError
from tgpanel.system.validation import scrub

log = logging.getLogger(__name__)

_USER_PROFILE_RE = re.compile(r"u[1-9][0-9]*")
_POOL_ENV_RE = re.compile(r"^([0-9]+)\.env$")

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

    @property
    def noop(self) -> bool:
        return not (
            self.changed
            or self.to_start
            or self.to_stop
            or self.nft_full
            or any(add or delete for _, add, delete in self.nft_changes)
            or self.relay_restart
        )


class _OperationTxn:
    """The operation transaction.

    File databases get a dedicated second connection: other writers (collector) are not dragged
    into the transaction, and readers on the main connection never see uncommitted rows. An
    in-memory database cannot be opened twice, so the shared connection is used there.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            files = self._db.call(
                lambda c: [str(r[2]) for r in c.execute("PRAGMA database_list") if r[1] == "main"]
            )
            path = files[0] if files else ""
            self._conn = connect(path) if path else self._db.conn
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
        self._txn_idle = asyncio.Event()
        self._txn_idle.set()
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

    async def db_write[R](self, fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> R:
        """Run a DB write for non-operation writers (collector, DB-only edits).

        Waits until no operation transaction is open, so the write is never blocked by (or
        swallowed into) an in-flight apply.
        """
        await self._txn_idle.wait()
        return await self.db.run(fn, *args, **kwargs)

    async def detect_drift(self) -> DriftReport | None:
        """Compare profiles.json with the hash we last wrote (read-only; used by doctor)."""
        report, _ = await self._drift()
        return report

    async def create_backup(self, reason: str = "manual", actor: str = "system") -> BackupInfo:
        """Manual backup (CLI/panel), serialised with applies via the global lock."""
        try:
            lock = await self.ops.acquire_lock(
                self.config.paths.lock, self.config.timing.lock_timeout_s
            )
        except SystemOpsError as exc:
            raise BackupError(f"не удалось получить блокировку: {_clean(str(exc))}") from None
        try:
            now = self._clock()
            snapshot = await asyncio.to_thread(backup_mod.db_snapshot_bytes, self.db)
            info = await backup_mod.create_backup(
                self.ops, self.config.paths, reason=reason, now=now, db_snapshot=snapshot
            )

            def record(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    backup_mod.record_backup(conn, info)
                    repo.add_audit(conn, now, actor, "backup.create", "", info.reason)

            await self.db_write(record)
            await self._prune()
            return info
        finally:
            with contextlib.suppress(Exception):
                await lock.release()

    async def sync_backups(self) -> int:
        """Register backups found on disk but unknown to the DB. Returns how many were added."""
        found = await backup_mod.scan_disk_backups(self.ops, self.config.paths)
        return await self.db_write(backup_mod.add_missing_backups, found)

    async def restore_backup(self, path: str, actor: str = "system") -> OperationOutcome[None]:
        """Restore the DB snapshot of an archive and reconcile the system through the pipeline.

        Proxy files are derived from the DB, so restoring the snapshot and re-rendering gives
        the archived state. A fresh pre-restore backup is taken by the pipeline itself; on
        failure everything (files, services, DB) returns to the pre-restore state.
        """
        try:
            archive = await backup_mod.read_archive(self.ops, path)
        except BackupError as exc:
            return OperationOutcome(ok=False, status="rejected", error=str(exc))
        if archive.db_snapshot is None:
            return OperationOutcome(
                ok=False,
                status="rejected",
                error="В архиве нет снимка базы данных: восстановление невозможно",
            )
        snapshot = archive.db_snapshot
        current_hash = await self._current_profiles_hash()
        current_names = await self._current_profile_names()

        def mutation(conn: sqlite3.Connection) -> None:
            backup_mod.restore_db_snapshot(conn, snapshot)
            # the file on disk is what we wrote: its managed names stay "ours" after the restore
            repo.set_setting(conn, KEY_ALL_NAMES, json.dumps(current_names[0]))
            repo.set_setting(conn, KEY_OUR_NAMES, json.dumps(current_names[1]))
            if current_hash is None:
                repo.delete_setting(conn, KEY_PROFILES_HASH)
            else:
                repo.set_setting(conn, KEY_PROFILES_HASH, current_hash)
            repo.add_audit(conn, self._clock(), actor, "backup.restore", "", "restore")

        return await self.run_operation(
            mutation,
            reason="restore",
            actor=actor,
            force_external=True,
            expect_profiles_hash=current_hash,
        )

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

    async def set_legacy_mtproxy(
        self, enabled: bool, actor: str = "system", *, force: bool = False
    ) -> str:
        """``tgpanel legacy-mtproxy on|off``: unmask+start / mask-now the legacy unit.

        Returns a short status text. Raises OperationRejected when refused.
        """
        cfg = self.config
        unit = cfg.paths.legacy_unit
        try:
            lock = await self.ops.acquire_lock(cfg.paths.lock, cfg.timing.lock_timeout_s)
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
                    facts = await read_live_facts(self.ops, cfg.paths)
                    encoded = facts_to_json(facts)
                    await self.db_write(self._save_facts, encoded)
                await self.ops.systemctl("mask-now", unit)
                text = "Старый процесс MTProxy остановлен и замаскирован"
                action = "legacy.off"
            else:
                await self.ops.systemctl("unmask", unit)
                await self.ops.systemctl("start", unit)
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
            await self._locked_batch(subs)
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

    async def _locked_batch(self, subs: list[_Submission]) -> None:
        started = self._clock()
        reason = self.current_reason or "apply"
        run_id: int = await self.db.run(repo.start_apply_run, started, reason)
        journal = _Journal()
        plan: _Plan | None = None
        backup: BackupInfo | None = None
        outcomes: dict[int, OperationOutcome[Any]] = {}  # index in subs -> outcome
        txn_open = False

        async def finish(
            status: str,
            error: str | None,
            *,
            keep_row: bool = True,
            audit: tuple[str, str] | None = None,
        ) -> None:
            await self._finalize(run_id, started, status, error, backup, keep_row, audit, subs)

        try:
            # ---- drift / expectations -------------------------------------------------
            report, current_hash = await self._drift()
            runnable: list[int] = []
            for i, sub in enumerate(subs):
                if report is not None and not sub.force_external:
                    exc = report.exception()
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="external_change",
                        error=(
                            "profiles.json изменён вне панели: "
                            f"{report.description}. Операция не выполнена."
                        ),
                        apply_run_id=run_id,
                        external=exc,
                    )
                elif sub.expect_hash is not None and sub.expect_hash != current_hash:
                    outcomes[i] = OperationOutcome(
                        ok=False,
                        status="rejected",
                        error="profiles.json изменился во время подготовки операции; повторите",
                    )
                else:
                    runnable.append(i)
            if report is not None and not runnable:
                await finish(
                    "failed",
                    _clean(f"внешнее изменение profiles.json: {report.description}"),
                    audit=("apply.external_change", _clean(report.description)),
                )
                self._publish(subs, outcomes)
                return
            if not runnable:
                await finish("rejected", None, keep_row=False)
                self._publish(subs, outcomes)
                return

            # ---- transaction + mutations --------------------------------------------
            await self._txn.begin()
            txn_open = True
            self._txn_idle.clear()
            conn = self._txn.conn
            values: dict[int, Any] = {}
            survivors: list[int] = []
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
                await self._txn.rollback()
                txn_open = False
                self._txn_idle.set()
                await finish("rejected", None, keep_row=False)
                self._publish(subs, outcomes)
                return

            # ---- plan, backup, validate, execute --------------------------------------
            force_nft = any(subs[i].full_nft_reload for i in survivors)
            plan = await self._build_plan(conn, force_nft)
            if plan.noop:
                self._store_hash(conn, plan)
                await self._txn.commit()
                txn_open = False
                self._txn_idle.set()
                await self._cleanup_stale(plan)
                await finish("noop", None, keep_row=False)
                for i in survivors:
                    outcomes[i] = OperationOutcome(
                        ok=True,
                        status="noop",
                        value=values[i],
                        warnings=tuple(plan.warnings),
                    )
                self._publish(subs, outcomes)
                return

            backup = await self._backup(plan, reason)
            await self._validate(plan)
            await self._execute(plan, journal)
            self._store_hash(conn, plan)
            try:
                await self._txn.commit()
            except sqlite3.Error as exc:
                raise ApplyError("commit", f"{type(exc).__name__}") from None
            txn_open = False
            self._txn_idle.set()
        except BaseException as exc:
            await self._fail(
                exc, subs, outcomes, run_id, plan, journal, txn_open, finish, backup is not None
            )
            if not isinstance(exc, Exception):
                raise
            return

        # ---- success ------------------------------------------------------------------
        if plan is None:  # pragma: no cover - unreachable, keeps mypy honest
            raise RuntimeError("plan missing after a successful apply")
        await self._cleanup_stale(plan)
        for i in survivors:
            outcomes[i] = OperationOutcome(
                ok=True,
                status="applied",
                value=values[i],
                apply_run_id=run_id,
                warnings=tuple(plan.warnings),
            )
        await finish("success", None)
        await self._prune()
        self._publish(subs, outcomes)

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
        txn_open: bool,
        finish: Callable[..., Awaitable[None]],
        had_backup: bool,
    ) -> None:
        if isinstance(exc, ApplyError):
            stage, detail = exc.stage, _clean(exc.detail)
        elif isinstance(exc, SystemOpsError):
            stage, detail = "internal", _clean(str(exc))
        elif isinstance(exc, (RenderError, BackupError)):
            stage, detail = "render", _clean(str(exc))
        elif isinstance(exc, asyncio.CancelledError):
            stage, detail = "internal", "применение прервано"
        else:
            log.warning("apply failed: %s", type(exc).__name__)
            stage, detail = "internal", type(exc).__name__
        rollback_errors = await asyncio.shield(self._rollback(plan, journal))
        if txn_open:
            await self._txn.rollback()
        self._txn_idle.set()
        text = (
            f"Не удалось применить изменения ({STAGE_RU.get(stage, stage)}): {detail}. "
            "Операция не выполнена, изменения отменены."
        )
        if rollback_errors:
            text += " ВНИМАНИЕ: откат выполнен не полностью, проверьте состояние сервисов."
        tech = f"[{stage}] {detail}"
        if rollback_errors:
            tech += " | rollback: " + "; ".join(rollback_errors)
        await finish(
            "failed",
            _clean(tech),
            audit=("apply.failed", _clean(tech)),
        )
        for i in range(len(subs)):
            if i in outcomes:  # rejected / external_change / own failure: keep as is
                continue
            outcomes[i] = OperationOutcome(
                ok=False,
                status="failed",
                error=text,
                apply_run_id=run_id,
                rolled_back=True,
                rollback_errors=tuple(rollback_errors),
            )
        self._publish(subs, outcomes)
        if self.on_failure is not None:
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
        _ = had_backup

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

    async def _build_plan(self, conn: sqlite3.Connection, force_nft: bool) -> _Plan:
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
        pm, po, pg = keep_or(snaps[paths.profiles], (0o400, "root", cfg.tproxy_group))
        targets[paths.profiles] = _Target(paths.profiles, rendered.profiles_json, pm, po, pg)
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
            for pool_id in rendered.pools_to_stop:
                if await ops.is_active(cfg.pool_unit_name(pool_id)):
                    to_stop.append(pool_id)
                env_path = paths.pool_env(pool_id)
                if await ops.exists(env_path):
                    stale.append(env_path)
            try:
                names = await ops.list_dir(paths.pools_dir)
            except SystemOpsError:
                names = []
            for name in names:
                m = _POOL_ENV_RE.match(name)
                if m is None or int(m.group(1)) in pools_by_id:
                    continue
                orphan = int(m.group(1))
                if await ops.is_active(cfg.pool_unit_name(orphan)):
                    to_stop.append(orphan)
                stale.append(f"{paths.pools_dir}/{name}")

            desired_ips = sorted({u.loopback_ip for u in active_users(state)})
            nft_full = force_nft
            nft_changes: list[tuple[str, list[str], list[str]]] = []
            if not nft_full:
                try:
                    for set_name in ("up", "down"):
                        current = set(await ops.nft_list_set("tgpanel", set_name))
                        want = set(desired_ips)
                        nft_changes.append(
                            (set_name, sorted(want - current), sorted(current - want))
                        )
                except SystemOpsError:
                    nft_full = True  # table is missing: load the whole file
                    nft_changes = []
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
        )

    @staticmethod
    def _differs(snap: _Snap, target: _Target) -> bool:
        if snap.data != target.data or snap.stat is None:
            return True
        return snap.stat.mode != target.mode

    async def _backup(self, plan: _Plan, reason: str) -> BackupInfo:
        try:
            # An in-memory DB shares one connection with the open transaction: the sqlite
            # backup API would wait for it forever, so no DB snapshot is taken in that mode.
            snapshot = (
                None
                if self._txn.shared
                else await asyncio.to_thread(backup_mod.db_snapshot_bytes, self.db)
            )
            return await backup_mod.create_backup(
                self.ops,
                self.config.paths,
                reason=reason,
                now=self._clock(),
                db_snapshot=snapshot,
            )
        except (BackupError, OSError, sqlite3.Error) as exc:
            raise ApplyError("backup", _clean(str(exc) or type(exc).__name__)) from None

    async def _validate(self, plan: _Plan) -> None:
        paths, ops = self.config.paths, self.ops
        if paths.profiles not in plan.changed and paths.config not in plan.changed:
            return
        cfg_t, prof_t = plan.targets[paths.config], plan.targets[paths.profiles]
        try:
            await ops.write_atomic(
                paths.check_config,
                cfg_t.data,
                mode=cfg_t.mode,
                owner=cfg_t.owner,
                group=cfg_t.group,
            )
            await ops.write_atomic(
                paths.check_profiles,
                prof_t.data,
                mode=prof_t.mode,
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
            raise ApplyError(
                "validate",
                "relay отклонил конфигурацию (возможно, не хватает памяти или превышен лимит): "
                + _clean(result.output),
            )

    # ------------------------------------------------------------------ execution

    async def _write(self, plan: _Plan, path: str, journal: _Journal) -> None:
        t = plan.targets[path]
        journal.written.append(path)  # recorded first: a half-failed write is still restored
        try:
            await self.ops.write_atomic(path, t.data, mode=t.mode, owner=t.owner, group=t.group)
        except SystemOpsError as exc:
            raise ApplyError("write", _clean(str(exc))) from None

    async def _systemctl(self, stage: str, action: str, unit: str) -> None:
        try:
            await self.ops.systemctl(action, unit)
        except SystemOpsError as exc:
            raise ApplyError(stage, _clean(str(exc))) from None

    async def _execute(self, plan: _Plan, journal: _Journal) -> None:
        cfg, paths, ops, timing = self.config, self.config.paths, self.ops, self.config.timing

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

        # C. nft: element differences only; the whole file only if the table is missing
        if paths.nft_file in plan.changed or plan.nft_full:
            await self._write(plan, paths.nft_file, journal)
        try:
            if plan.nft_full:
                await ops.nft_load_file(paths.nft_file)
            else:
                for set_name, add, delete in plan.nft_changes:
                    if add:
                        await ops.nft_add_elements("tgpanel", set_name, add)
                        journal.nft_added.append((set_name, add))
                    if delete:
                        await ops.nft_delete_elements("tgpanel", set_name, delete)
                        journal.nft_deleted.append((set_name, delete))
        except SystemOpsError as exc:
            raise ApplyError("nft", _clean(str(exc))) from None

        # D. relay files (after pools), then the relay
        if paths.config in plan.changed:
            await self._write(plan, paths.config, journal)
        if paths.profiles in plan.changed:
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

    async def _cleanup_stale(self, plan: _Plan) -> None:
        for path in plan.stale_envs:
            try:
                await self.ops.remove(path)
            except SystemOpsError:
                log.warning("could not remove stale pool env file")

    # ------------------------------------------------------------------ rollback

    async def _rollback(self, plan: _Plan | None, journal: _Journal) -> list[str]:
        errors: list[str] = []
        cfg, ops, timing = self.config, self.ops, self.config.timing

        async def step(label: str, coro: Awaitable[Any]) -> Any:
            try:
                return await coro
            except Exception as exc:
                errors.append(f"{label}: {_clean(str(exc) or type(exc).__name__)}")
                return None

        if plan is not None:
            for path in reversed(journal.written):
                snap = plan.snaps[path]
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
            if journal.unit_written:
                await step("daemon-reload", ops.systemctl("daemon-reload", ""))
        for set_name, ips in reversed(journal.nft_added):
            await step(f"nft delete {set_name}", ops.nft_delete_elements("tgpanel", set_name, ips))
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
