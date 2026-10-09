"""Composition helper: build the apply pipeline and services around one database."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.importer import Importer
from tgpanel.apply.pipeline import ApplyFailure, ApplyPipeline, RecoveryReport
from tgpanel.apply.runtime import ensure_runtime_dirs
from tgpanel.db.connection import Database
from tgpanel.services.settings_service import SettingsServiceImpl
from tgpanel.services.users import UserServiceImpl
from tgpanel.system.ops import SystemOps


@dataclass
class AppContext:
    db: Database
    pipeline: ApplyPipeline
    users: UserServiceImpl
    settings: SettingsServiceImpl
    importer: Importer

    async def start(self, *, recover: bool = True) -> RecoveryReport | None:
        """Service start: runtime directories, crash recovery, cached proxy host name.

        ``recover=False`` (CLI) skips the crash recovery that restores files of an interrupted
        apply; the service always runs it once before accepting operations.
        """
        await ensure_runtime_dirs(self.pipeline.ops, self.pipeline.config.paths)
        report = await self.pipeline.startup_recovery() if recover else None
        await self.users.load_hostname()
        return report

    def close(self) -> None:
        self.pipeline.close()
        self.db.close()


def build_context(
    ops: SystemOps,
    db_path: str | Path,
    *,
    config: ApplyConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_failure: Callable[[ApplyFailure], Awaitable[None]] | None = None,
) -> AppContext:
    db = Database(db_path)
    pipeline = ApplyPipeline(ops, db, config, clock=clock, sleep=sleep, on_failure=on_failure)
    users = UserServiceImpl(pipeline, db)

    async def refresh_hostname() -> None:
        await users.load_hostname()

    return AppContext(
        db=db,
        pipeline=pipeline,
        users=users,
        settings=SettingsServiceImpl(pipeline, db, on_change=refresh_hostname),
        importer=Importer(pipeline),
    )
