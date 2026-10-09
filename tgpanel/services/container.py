"""Composition helper: build the apply pipeline and services around one database."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.importer import Importer
from tgpanel.apply.pipeline import ApplyFailure, ApplyPipeline
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
    return AppContext(
        db=db,
        pipeline=pipeline,
        users=UserServiceImpl(pipeline, db),
        settings=SettingsServiceImpl(pipeline, db),
        importer=Importer(pipeline),
    )
