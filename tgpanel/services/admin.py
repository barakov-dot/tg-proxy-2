"""Panel administration: bot admins, bot token (env file), message templates, panel password."""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo

KEY_LOGIN = "panel_login"
KEY_HASH = "panel_password_hash"
KEY_VERSION = "panel_session_version"
KEY_TOKEN_SET_AT = "bot_token_set_at"  # noqa: S105 - setting name; the token itself is never stored
ENV_BOT_TOKEN = "TGPANEL_BOT_TOKEN"  # noqa: S105 - env variable name
MESSAGE_KEYS = (
    "msg.link",
    "msg.welcome",
    "msg.approved",
    "msg.rejected",
    "msg.expiring",
    "msg.expired",
    "msg.broadcast",
)
MAX_TEMPLATE = 3500
MIN_PASSWORD = 12
MAX_PASSWORD = 1024
_TOKEN_RE = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,60}")
_SECRET_LIKE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")

WriteEnv = Callable[[str, str], Awaitable[None] | None]


class AdminService:
    def __init__(
        self,
        pipeline: ApplyPipeline,
        *,
        write_env: WriteEnv | None = None,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._db = pipeline.db
        self._write_env = write_env
        self._hasher = hasher or PasswordHasher()

    async def audit(self, actor: str, action: str, target: str = "", details: str = "") -> None:
        """Append an audit entry (``OperationRejected`` if the database stays busy)."""

        def write(conn: sqlite3.Connection) -> None:
            repo.add_audit(conn, self._pipeline.now(), actor, action, target, details)

        await self._pipeline.db_write(write)

    # ------------------------------------------------------------------ passwords

    async def verify(self, stored_hash: str, password: str) -> bool:
        """argon2 verification off the event loop."""
        try:
            return bool(await asyncio.to_thread(self._hasher.verify, stored_hash, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    async def hash(self, password: str) -> str:
        return str(await asyncio.to_thread(self._hasher.hash, password))

    async def change_password(self, current: str, new: str, again: str, actor: str) -> int:
        """Change the panel password; returns the new session version (others are logged out)."""
        stored = await self._db.run(repo.get_setting, KEY_HASH, "")
        if not stored or not await self.verify(stored, current):
            raise OperationRejected("Текущий пароль неверен")
        if len(new) < MIN_PASSWORD or len(new) > MAX_PASSWORD:
            raise OperationRejected(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
        if new != again:
            raise OperationRejected("Пароли не совпадают")
        new_hash = await self.hash(new)
        return await self._bump(actor, new_hash, "web.password_change")

    async def logout_all(self, actor: str) -> int:
        return await self._bump(actor, None, "web.logout_all")

    async def _bump(self, actor: str, new_hash: str | None, action: str) -> int:
        def write(conn: sqlite3.Connection) -> int:
            raw = repo.get_setting(conn, KEY_VERSION, "1") or "1"
            version = (int(raw) if raw.isascii() and raw.isdigit() else 1) + 1
            repo.set_setting(conn, KEY_VERSION, str(version))
            if new_hash is not None:
                repo.set_setting(conn, KEY_HASH, new_hash)
            repo.add_audit(conn, self._pipeline.now(), actor, action, "", "")
            return version

        return await self._pipeline.db_write(write)

    # ------------------------------------------------------------------ bot admins

    async def list_admins(self) -> list[int]:
        return await self._db.run(repo.list_admins)

    async def add_admin(self, tg_id: int, actor: str) -> None:
        await self._admin(tg_id, actor, add=True)

    async def remove_admin(self, tg_id: int, actor: str) -> None:
        await self._admin(tg_id, actor, add=False)

    async def _admin(self, tg_id: int, actor: str, *, add: bool) -> None:
        if not 0 < tg_id <= 2**53:
            raise OperationRejected("Некорректный Telegram ID")

        def write(conn: sqlite3.Connection) -> None:
            now = self._pipeline.now()
            if add:
                repo.add_admin(conn, tg_id, now)
            else:
                repo.remove_admin(conn, tg_id)
            repo.add_audit(conn, now, actor, "admin.add" if add else "admin.remove", f"tg:{tg_id}")

        await self._pipeline.db_write(write)

    # ------------------------------------------------------------------ bot token

    async def bot_token_set_at(self) -> str | None:
        return await self._db.run(repo.get_setting, KEY_TOKEN_SET_AT, None)

    async def set_bot_token(self, token: str, actor: str) -> None:
        """Write the token to the env file via the injected ``write_env`` (never to the DB)."""
        if not _TOKEN_RE.fullmatch(token):
            raise OperationRejected("Токен имеет неверный формат")
        if self._write_env is None:
            raise OperationRejected("Запись токена не настроена на этом сервере")
        try:
            result: Any = self._write_env(ENV_BOT_TOKEN, token)
            if inspect.isawaitable(result):
                await result
        except OSError:
            raise OperationRejected("Не удалось записать токен в файл окружения") from None

        def write(conn: sqlite3.Connection) -> None:
            now = self._pipeline.now()
            repo.delete_setting(conn, "bot_token")  # legacy plaintext copy, if any
            repo.set_setting(conn, KEY_TOKEN_SET_AT, now.strftime("%Y-%m-%d %H:%M UTC"))
            repo.add_audit(conn, now, actor, "settings.set", "bot_token", "***")

        await self._pipeline.db_write(write)

    # ------------------------------------------------------------------ templates

    async def templates(self) -> dict[str, str]:
        stored = await self._db.run(repo.all_settings)
        return {k: stored.get(k, "") for k in MESSAGE_KEYS}

    async def set_templates(self, values: Mapping[str, str], actor: str) -> None:
        clean: dict[str, str] = {}
        for key in MESSAGE_KEYS:
            text = values.get(key, "").strip()
            if len(text) > MAX_TEMPLATE or _SECRET_LIKE.search(text):
                raise OperationRejected("Шаблон слишком длинный или содержит секрет")
            clean[key] = text

        def write(conn: sqlite3.Connection) -> None:
            for key, text in clean.items():
                if text:
                    repo.set_setting(conn, key, text)
                else:
                    repo.delete_setting(conn, key)
            repo.add_audit(conn, self._pipeline.now(), actor, "settings.templates", "", "")

        await self._pipeline.db_write(write)
