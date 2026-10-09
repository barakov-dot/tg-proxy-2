"""Typed settings service: validated get/set for the keys used by the panel.

Keys that influence the proxy-side files (limits, secrets_per_process, carrier mode...) are
changed through a pipeline operation (re-render + apply, rolled back on failure); the others
are plain audited DB writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from tgpanel.apply.errors import OperationRejected, SettingsError
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import SPECS, AppSettings, SettingSpec, normalize, read_settings
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.pools import occupancy
from tgpanel.services.api import Actor, OperationResult


class SettingsServiceImpl:
    def __init__(self, pipeline: ApplyPipeline, db: Database) -> None:
        self._pipeline = pipeline
        self._db = db

    @staticmethod
    def specs() -> Mapping[str, SettingSpec]:
        return SPECS

    async def snapshot(self) -> AppSettings:
        return await self._db.run(read_settings)

    async def get(self, key: str) -> Any:
        if key not in SPECS:
            raise SettingsError("Неизвестная настройка")
        cfg = await self.snapshot()
        return getattr(cfg, key)

    async def all(self) -> dict[str, Any]:
        cfg = await self.snapshot()
        return {key: getattr(cfg, key) for key in SPECS}

    @staticmethod
    def validate(values: Mapping[str, object]) -> dict[str, str]:
        """Canonical stored strings for ``values`` (raises SettingsError)."""
        return {key: normalize(key, raw) for key, raw in values.items()}

    async def set(self, key: str, value: object, actor: Actor) -> OperationResult:
        return await self.set_many({key: value}, actor)

    async def set_many(self, values: Mapping[str, object], actor: Actor) -> OperationResult:
        """Validate and store settings; ONE apply if any of them affects the proxy files."""
        try:
            canonical = self.validate(values)
        except SettingsError as exc:
            return OperationResult(ok=False, error=str(exc))
        if not canonical:
            return OperationResult(ok=True)
        affects_proxy = any(SPECS[k].affects_proxy for k in canonical)

        def write(conn: sqlite3.Connection) -> list[int]:
            now = self._pipeline.now()
            current = repo.all_settings(conn)
            spp = canonical.get("secrets_per_process")
            if spp is not None:
                counts = occupancy(repo.list_pools(conn), repo.all_users(conn))
                busiest = max(counts.values(), default=0)
                if int(spp) < busiest:
                    raise OperationRejected(
                        f"Нельзя поставить {spp}: в одном из пулов уже {busiest} секретов"
                    )
            for key, value in canonical.items():
                if current.get(key) != value:
                    repo.set_setting(conn, key, value)
                    repo.add_audit(conn, now, actor, "settings.set", key, value)
            return []

        if affects_proxy:
            outcome = await self._pipeline.run_operation(write, reason="settings", actor=actor)
            if not outcome.ok:
                return OperationResult(
                    ok=False, error=outcome.error, apply_run_id=outcome.apply_run_id
                )
            return OperationResult(ok=True, apply_run_id=outcome.apply_run_id)

        def plain(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                write(conn)

        try:
            await self._pipeline.db_write(plain)
        except OperationRejected as exc:
            return OperationResult(ok=False, error=str(exc))
        return OperationResult(ok=True)
