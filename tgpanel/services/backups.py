"""Backups for the web layer: list, create, audited download, restore (all through the pipeline)."""

from __future__ import annotations

import posixpath
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass

from tgpanel.apply.backup import BackupError
from tgpanel.apply.pipeline import ApplyPipeline, OperationOutcome
from tgpanel.db import repo
from tgpanel.system.ops import SystemOpsError

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class BackupDownload:
    filename: str
    size: int
    chunks: AsyncIterator[bytes]


class BackupService:
    def __init__(self, pipeline: ApplyPipeline, *, max_download: int = MAX_DOWNLOAD_BYTES) -> None:
        self._pipeline = pipeline
        self._max = max_download

    async def list(self) -> list[repo.BackupRecord]:
        return await self._pipeline.db.run(repo.list_backups)

    async def create(self, actor: str) -> None:
        """Full backup (raises ``BackupError`` / ``OperationRejected``)."""
        await self._pipeline.create_backup("manual", actor, full=True)

    async def open_download(self, backup_id: int, actor: str) -> BackupDownload | None:
        """The archive of a registered backup inside the backup directory, or None.

        The download is audited (``backup.download``) before any byte is sent. The archive is
        capped at ``max_download`` bytes and handed out in 1 MiB chunks.
        """
        rec = await self._pipeline.db.run(repo.get_backup, backup_id)
        base = self._pipeline.config.paths.backups_dir.rstrip("/") + "/"
        if rec is None or posixpath.normpath(rec.path) != rec.path or not rec.path.startswith(base):
            return None
        ops = self._pipeline.ops
        try:
            size = (await ops.stat(rec.path)).size
        except SystemOpsError:
            return None
        if size > self._max:
            raise BackupError("Архив слишком большой для скачивания через панель")
        try:
            data = await ops.read_file(rec.path)
        except SystemOpsError:
            return None

        def audit(conn: sqlite3.Connection) -> None:
            repo.add_audit(
                conn, self._pipeline.now(), actor, "backup.download", f"backup:{rec.id}", ""
            )

        await self._pipeline.db_write(audit)

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(data), CHUNK):
                yield data[i : i + CHUNK]

        filename = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(rec.path))
        return BackupDownload(filename, len(data), chunks())

    async def restore(self, backup_id: int, actor: str) -> OperationOutcome[None]:
        return await self._pipeline.restore_backup(backup_id, actor)
