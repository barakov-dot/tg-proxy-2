"""Backups for the web layer: list, create, audited block-wise download, restore."""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import BinaryIO

from tgpanel.apply.backup import BackupError
from tgpanel.apply.pipeline import ApplyPipeline, OperationOutcome
from tgpanel.db import repo

MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
CHUNK = 1 << 20
STALE_SLOT_S = 1800.0

BackupOpener = Callable[[str], BinaryIO]


class DownloadBusy(Exception):
    """Another download is already running (only one at a time)."""


def open_local(path: str, base_dir: str) -> BinaryIO:
    """Open a backup archive on the local disk (symlinks must not leave the backup directory)."""
    real_base = os.path.realpath(base_dir)
    if not (os.path.realpath(path) + "/").startswith(real_base.rstrip("/") + "/"):
        raise FileNotFoundError(path)
    return open(path, "rb")


@dataclass(slots=True)
class BackupDownload:
    filename: str
    size: int
    chunks: AsyncIterator[bytes]
    _release: Callable[[], None] = field(repr=False)

    def release(self) -> None:
        """Free the single download slot (idempotent)."""
        self._release()


class BackupService:
    def __init__(
        self,
        pipeline: ApplyPipeline,
        *,
        max_download: int = MAX_DOWNLOAD_BYTES,
        opener: BackupOpener | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pipeline = pipeline
        self._max = max_download
        self._opener = opener
        self._mono = monotonic
        self._busy_since: float | None = None

    async def list(self) -> list[repo.BackupRecord]:
        return await self._pipeline.db.run(repo.list_backups)

    async def create(self, actor: str) -> None:
        """Full backup (raises ``BackupError`` / ``OperationRejected``)."""
        await self._pipeline.create_backup("manual", actor, full=True)

    def _take_slot(self) -> None:
        now = self._mono()
        if self._busy_since is not None and now - self._busy_since < STALE_SLOT_S:
            raise DownloadBusy
        self._busy_since = now

    def _free_slot(self, token: float) -> None:
        if self._busy_since == token:
            self._busy_since = None

    async def open_download(self, backup_id: int, actor: str) -> BackupDownload | None:
        """The archive of a registered backup inside the backup directory, or None.

        Only one download runs at a time (``DownloadBusy`` otherwise). The file is read in 1 MiB
        blocks in a worker thread - never whole into memory - and the download is audited
        (``backup.download``) before any byte is sent.
        """
        rec = await self._pipeline.db.run(repo.get_backup, backup_id)
        base = self._pipeline.config.paths.backups_dir
        prefix = base.rstrip("/") + "/"
        if (
            rec is None
            or posixpath.normpath(rec.path) != rec.path
            or not rec.path.startswith(prefix)
        ):
            return None
        opener = self._opener or (lambda p: open_local(p, base))
        self._take_slot()
        token = self._busy_since or 0.0
        try:
            try:
                handle = await asyncio.to_thread(opener, rec.path)
            except OSError:
                self._free_slot(token)
                return None
            size = await asyncio.to_thread(_size_of, handle)
            if size > self._max:
                await asyncio.to_thread(handle.close)
                raise BackupError("Архив слишком большой для скачивания через панель")

            def audit(conn: sqlite3.Connection) -> None:
                repo.add_audit(
                    conn, self._pipeline.now(), actor, "backup.download", f"backup:{rec.id}", ""
                )

            try:
                await self._pipeline.db_write(audit)
            except BaseException:
                await asyncio.to_thread(handle.close)
                raise
        except BaseException:
            self._free_slot(token)
            raise

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    block = await asyncio.to_thread(handle.read, CHUNK)
                    if not block:
                        return
                    yield block
            finally:
                await asyncio.to_thread(handle.close)
                self._free_slot(token)

        def release() -> None:
            handle.close()
            self._free_slot(token)

        filename = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(rec.path))
        return BackupDownload(filename, size, chunks(), release)

    async def restore(self, backup_id: int, actor: str) -> OperationOutcome[None]:
        return await self._pipeline.restore_backup(backup_id, actor)


def _size_of(handle: BinaryIO) -> int:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    return size
