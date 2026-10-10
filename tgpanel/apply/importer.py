"""Import of the proxy's existing profiles into the panel (PLAN 3.10).

``preview`` is read-only. ``confirm`` is ONE pipeline operation: the pools are started and their
ports awaited BEFORE profiles.json is rewritten and the relay restarted (the pipeline always
orders pool changes first), and any failure rolls back everything, leaving the old behaviour.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import read_settings
from tgpanel.db import repo
from tgpanel.domain.addresses import QUARANTINE_KEY, allocate_addresses, parse_quarantine
from tgpanel.domain.import_ import (
    DEFAULT_ID_REGEX,
    CsvRow,
    ImportPlan,
    PlanRow,
    SourceProfile,
    parse_csv_rows,
    parse_mtproxy_secrets_text,
    plan_import,
)
from tgpanel.domain.models import UserStatus
from tgpanel.domain.pools import allocate_pools
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import parse_profiles, profiles_hash
from tgpanel.system.ops import SystemOpsError

_ENV_SECRET_RE = re.compile(
    r"^\s*(?:export\s+)?MTPROXY_SECRET=[\"']?([0-9A-Fa-f]{32,34})[\"']?\s*$", re.M
)

OLD_BOT_WARNING = (
    "Прежний бот должен быть остановлен: иначе он перезапишет profiles.json своей версией."
)


class ImportSourceError(Exception):
    """Source files cannot be read (message is secret-free)."""


@dataclass(frozen=True, slots=True)
class RowEdit:
    """User edits of one preview row (keyed by the source profile name)."""

    tg_id: int | None = None
    display_name: str | None = None  # the user's display name ("Имя"), not the profile name
    comment: str | None = None
    skip: bool = False  # do not import: the profile stays an unmanaged pass-through entry


@dataclass(frozen=True, slots=True)
class PreviewRow:
    row: PlanRow
    pool_id: int | None  # None for skipped rows
    pool_port: int | None
    loopback_ip: str | None
    new_pool: bool
    name_conflict: bool


@dataclass(frozen=True, slots=True)
class _Sources:
    profiles: tuple[SourceProfile, ...] = field(repr=False)
    mtproxy_secrets: frozenset[str] | None = field(repr=False)
    csv_rows: tuple[CsvRow, ...]
    id_regex: str
    profiles_hash: str | None


@dataclass(frozen=True, slots=True)
class ImportPreview:
    plan: ImportPlan
    rows: tuple[PreviewRow, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    drift: str | None  # description of an external change of profiles.json (with baseline)
    sources: _Sources = field(repr=False)

    @property
    def importable(self) -> int:
        return sum(1 for r in self.rows if r.row.will_import)

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True, slots=True)
class ImportResult:
    ok: bool
    imported: int = 0
    skipped: int = 0
    user_ids: tuple[int, ...] = ()
    error: str | None = None
    apply_run_id: int | None = None
    warnings: tuple[str, ...] = ()


class Importer:
    def __init__(self, pipeline: ApplyPipeline) -> None:
        self._pipeline = pipeline
        self._ops = pipeline.ops
        self._db = pipeline.db
        self._paths = pipeline.config.paths

    # ------------------------------------------------------------------ reading

    async def _read_sources(
        self, csv_text: str | None, id_regex: str
    ) -> tuple[_Sources, list[str]]:
        csv_errors: list[str] = []
        try:
            data = await self._ops.read_file(self._paths.profiles)
            entries = parse_profiles(data)
            digest = profiles_hash(data)
        except SystemOpsError:
            raise ImportSourceError("Не удалось прочитать profiles.json") from None
        except RenderError as exc:
            raise ImportSourceError(f"profiles.json не разобран: {exc}") from None
        profiles = tuple(
            SourceProfile(e.name, e.secret, e.carrier_mode, e.backend) for e in entries
        )

        found = False
        secrets: set[str] = set()
        try:
            if await self._ops.exists(self._paths.mtproxy_secrets):
                text = (await self._ops.read_file(self._paths.mtproxy_secrets)).decode(
                    "utf-8", "replace"
                )
                secrets |= parse_mtproxy_secrets_text(text)
                found = True
            if await self._ops.exists(self._paths.mtproxy_env):
                env = (await self._ops.read_file(self._paths.mtproxy_env)).decode(
                    "utf-8", "replace"
                )
                for match in _ENV_SECRET_RE.finditer(env):
                    secrets.add(match.group(1).lower())
                    found = True
        except SystemOpsError:
            found = bool(secrets)
        csv_rows: list[CsvRow] = []
        if csv_text:
            csv_rows, csv_errors = parse_csv_rows(csv_text)
        return (
            _Sources(
                profiles, frozenset(secrets) if found else None, tuple(csv_rows), id_regex, digest
            ),
            csv_errors,
        )

    @staticmethod
    def _plan(conn: sqlite3.Connection, src: _Sources, extra_csv: tuple[CsvRow, ...]) -> ImportPlan:
        users = repo.all_users(conn)
        return plan_import(
            src.profiles,
            mtproxy_secrets=src.mtproxy_secrets,
            existing_secrets=[u.secret for u in users],
            existing_tg_ids=[u.tg_id for u in users if u.tg_id is not None],
            id_regex=src.id_regex,
            csv_rows=[*src.csv_rows, *extra_csv],
        )

    # ------------------------------------------------------------------ preview

    async def preview(
        self, *, csv_text: str | None = None, id_regex: str = DEFAULT_ID_REGEX
    ) -> ImportPreview:
        """Read-only plan: table of source name -> Telegram ID -> name -> pool -> address."""
        sources, csv_errors = await self._read_sources(csv_text, id_regex)
        return await self._build_preview(sources, csv_errors, ())

    async def _build_preview(
        self, sources: _Sources, csv_errors: list[str], extra_csv: tuple[CsvRow, ...]
    ) -> ImportPreview:
        def work(conn: sqlite3.Connection) -> ImportPreview:
            plan = self._plan(conn, sources, extra_csv)
            cfg = read_settings(conn)
            pools = repo.list_pools(conn)
            users = repo.all_users(conn)
            importable = [r for r in plan.rows if r.will_import]
            alloc = allocate_pools(pools, users, len(importable), cfg.secrets_per_process)
            by_id = {p.id: p for p in [*pools, *alloc.new_pools]}
            quarantined = parse_quarantine(
                repo.get_setting(conn, QUARANTINE_KEY), self._pipeline.now()
            )
            ips = allocate_addresses(repo.used_loopback_ips(conn), len(importable), quarantined)
            existing_names = {u.name for u in users}
            new_ids = {p.id for p in alloc.new_pools}
            rows: list[PreviewRow] = []
            it = iter(zip(alloc.pool_ids, ips, strict=True))
            for row in plan.rows:
                if not row.will_import:
                    rows.append(PreviewRow(row, None, None, None, False, False))
                    continue
                pool_id, ip = next(it)
                rows.append(
                    PreviewRow(
                        row,
                        pool_id,
                        by_id[pool_id].port,
                        ip,
                        pool_id in new_ids,
                        row.name in existing_names,
                    )
                )
            errors = [*plan.errors, *csv_errors]
            seen_names: set[str] = set()
            for pr in rows:
                if pr.row.will_import:
                    if pr.row.name in seen_names:
                        errors.append(f"имя «{pr.row.name}» встречается дважды")
                    seen_names.add(pr.row.name)
            for pr in rows:
                if pr.name_conflict:
                    errors.append(f"{pr.row.source_name}: имя «{pr.row.name}» уже занято")
            warnings = [OLD_BOT_WARNING, *plan.warnings]
            return ImportPreview(plan, tuple(rows), tuple(warnings), tuple(errors), None, sources)

        preview = await self._db.run(work)
        report = await self._pipeline.detect_drift()
        drift = None if report is None or report.no_baseline else report.description
        if drift:
            preview = replace(
                preview,
                drift=drift,
                warnings=(
                    *preview.warnings,
                    f"profiles.json изменялся вне панели: {drift}",
                ),
            )
        return preview

    # ------------------------------------------------------------------ confirm

    async def confirm(
        self,
        preview: ImportPreview,
        edits: Mapping[str, RowEdit] | None = None,
        *,
        actor: str = "system",
    ) -> ImportResult:
        """ONE operation: pools -> users -> profiles.json -> relay. Idempotent."""
        skipped_names = {n for n, e in (edits or {}).items() if e.skip}
        sources = replace(
            preview.sources,
            profiles=tuple(p for p in preview.sources.profiles if p.name not in skipped_names),
        )
        overrides = self._overrides(preview.plan.rows, edits or {})
        # Re-plan with the user's edits; the mutation re-plans once more in the transaction.
        replanned = await self._build_preview(sources, [], overrides)
        if replanned.blocked:
            return ImportResult(ok=False, error="; ".join(replanned.errors))
        todo = replanned.importable
        skipped = len(replanned.rows) - todo + len(skipped_names)
        if todo == 0:
            return ImportResult(ok=True, imported=0, skipped=skipped, warnings=replanned.warnings)

        def mutation(conn: sqlite3.Connection) -> list[int]:
            return self._insert_users(conn, sources, overrides, actor)

        outcome = await self._pipeline.run_operation(
            mutation,
            reason="import",
            actor=actor,
            force_external=True,
            expect_profiles_hash=sources.profiles_hash,
        )
        if not outcome.ok:
            return ImportResult(
                ok=False, error=outcome.error, apply_run_id=outcome.apply_run_id, skipped=skipped
            )
        ids = tuple(outcome.value or ())
        return ImportResult(
            ok=True,
            imported=len(ids),
            skipped=skipped,
            user_ids=ids,
            apply_run_id=outcome.apply_run_id,
            warnings=replanned.warnings,
        )

    @staticmethod
    def _overrides(rows: tuple[PlanRow, ...], edits: Mapping[str, RowEdit]) -> tuple[CsvRow, ...]:
        by_name = {r.source_name: r for r in rows}
        out: list[CsvRow] = []
        for name, edit in edits.items():
            row = by_name.get(name)
            if row is None:
                continue
            if edit.comment is not None:
                comment = edit.comment.strip()
            elif row.comment.startswith("import; "):
                comment = row.comment[len("import; ") :]
            else:
                comment = ""
            out.append(
                CsvRow(
                    profile_name=name,
                    tg_id=row.tg_id if edit.tg_id is None else edit.tg_id,
                    display_name=row.display_name
                    if edit.display_name is None
                    else edit.display_name.strip(),
                    comment=comment,
                )
            )
        # later CsvRow for the same profile wins (dict in plan_import), so edits override.
        return tuple(out)

    def _insert_users(
        self,
        conn: sqlite3.Connection,
        sources: _Sources,
        overrides: tuple[CsvRow, ...],
        actor: str,
    ) -> list[int]:
        plan = self._plan(conn, sources, overrides)
        if plan.errors:
            raise OperationRejected("Импорт невозможен: " + "; ".join(plan.errors))
        rows = list(plan.importable)
        if not rows:
            return []
        now = self._pipeline.now()
        cfg = read_settings(conn)
        pools = repo.list_pools(conn)
        users = repo.all_users(conn)
        alloc = allocate_pools(pools, users, len(rows), cfg.secrets_per_process)
        for pool in alloc.new_pools:
            repo.insert_pool(conn, pool, now)
        quarantined = parse_quarantine(repo.get_setting(conn, QUARANTINE_KEY), now)
        ips = allocate_addresses(repo.used_loopback_ips(conn), len(rows), quarantined)
        taken = {u.name for u in users}
        ids: list[int] = []
        for row, pool_id, ip in zip(rows, alloc.pool_ids, ips, strict=True):
            if row.name in taken:
                raise OperationRejected(f"Имя «{row.name}» уже занято")
            taken.add(row.name)
            uid = repo.insert_user(
                conn,
                name=row.name,
                display_name=row.display_name,
                secret=row.secret,
                status=UserStatus.ACTIVE,
                pool_id=pool_id,
                loopback_ip=ip,
                created_at=now,
                carrier_mode=row.carrier_mode,
                expires_at=None,
                comment=row.comment,
                tg_id=row.tg_id,
                imported=True,
                source_profile_name=row.source_name,
            )
            ids.append(uid)
            repo.add_audit(conn, now, actor, "user.import", f"user:{uid}", f"pool={pool_id}")
        repo.add_audit(conn, now, actor, "import.confirm", "", f"imported={len(ids)}")
        return ids

    # ------------------------------------------------------------------ legacy process

    async def legacy_mtproxy(
        self, enabled: bool, actor: str = "system", *, force: bool = False
    ) -> str:
        """``on``: unmask+start the old process; ``off``: mask --now (after a successful import)."""
        return await self._pipeline.set_legacy_mtproxy(enabled, actor, force=force)
