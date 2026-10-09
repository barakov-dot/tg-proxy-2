"""Batched DB-only edits used by the web layer (one write lock, one transaction)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo
from tgpanel.db.connection import transaction
from tgpanel.services.errors import UserServiceError

MAX_COMMENT = 2000


class BulkService:
    def __init__(self, pipeline: ApplyPipeline) -> None:
        self._pipeline = pipeline

    async def set_comment(self, ids: Sequence[int], comment: str, actor: str) -> int:
        """Set the comment of many users in ONE transaction. Returns how many were changed."""
        if len(comment) > MAX_COMMENT:
            raise UserServiceError(f"Комментарий длиннее {MAX_COMMENT} символов")
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise UserServiceError("Не выбрано ни одного пользователя")

        def work(conn: sqlite3.Connection) -> int:
            with transaction(conn):
                users = repo.users_by_ids(conn, unique)
                now = self._pipeline.now()
                for user in users:
                    repo.update_user(conn, user.id, comment=comment)
                    repo.add_audit(
                        conn, now, actor, "user.update_meta", f"user:{user.id}", "comment"
                    )
                return len(users)

        return await self._pipeline.db_write(work)

    async def clear_tg_id(self, user_id: int, actor: str) -> None:
        """Unlink the Telegram ID (``UserService.update_meta`` cannot clear it)."""

        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                if repo.get_user(conn, user_id) is None:
                    raise UserServiceError("Пользователь не найден")
                repo.update_user(conn, user_id, tg_id=None)
                repo.add_audit(
                    conn,
                    self._pipeline.now(),
                    actor,
                    "user.update_meta",
                    f"user:{user_id}",
                    "tg_id",
                )

        await self._pipeline.db_write(work)
