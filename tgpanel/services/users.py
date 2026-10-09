"""UserService implementation (contract: services/api.py).

Every mutating call is ONE pipeline operation (one apply, however many users it touches). The
DB mutation runs inside the operation transaction, so a failed apply leaves no trace in the DB.
Links appear in the result only after a successful apply.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import AppSettings, read_settings
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.addresses import allocate_addresses
from tgpanel.domain.expiry import (
    Term,
    default_expiry,
    due_for_expiry,
    extend_expiry,
    status_after_extend,
)
from tgpanel.domain.models import CarrierMode, UserRecord, UserStatus
from tgpanel.domain.pools import allocate_pools
from tgpanel.domain.secrets_ import base_secret, generate_secret
from tgpanel.render.links import https_link, tg_link
from tgpanel.services.api import (
    Actor,
    NewUser,
    OperationResult,
    UserListQuery,
    UserPage,
    UserRow,
)
from tgpanel.services.errors import UserServiceError

MAX_NAME = 100
MAX_COMMENT = 2000
MAX_EXTEND_DAYS = 3650


def _ids_text(ids: Sequence[int]) -> str:
    shown = ", ".join(str(i) for i in ids[:10])
    return shown + (f" и ещё {len(ids) - 10}" if len(ids) > 10 else "")


def _check_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > MAX_NAME or "\n" in value or "\r" in value:
        raise UserServiceError(f"Имя должно быть от 1 до {MAX_NAME} символов в одну строку")
    return value


def _check_comment(comment: str) -> str:
    if len(comment) > MAX_COMMENT:
        raise UserServiceError(f"Комментарий длиннее {MAX_COMMENT} символов")
    return comment


def _check_tg_id(tg_id: int) -> int:
    if tg_id <= 0 or tg_id > 2**53:
        raise UserServiceError("Некорректный Telegram ID")
    return tg_id


def default_term_expiry(cfg: AppSettings, now: datetime) -> datetime:
    """Expiry for a new user from the ``default_term`` setting."""
    term = Term(cfg.default_term)
    explicit: datetime | None = None
    if term is Term.DATE:
        if not cfg.default_term_date:
            raise OperationRejected("Не задана дата срока по умолчанию")
        d = date.fromisoformat(cfg.default_term_date)
        explicit = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
    expiry = default_expiry(term, now, explicit)
    if expiry <= now:
        raise OperationRejected("Срок по умолчанию уже истёк: измените настройку")
    return expiry


class UserServiceImpl:
    def __init__(self, pipeline: ApplyPipeline, db: Database) -> None:
        self._pipeline = pipeline
        self._db = db
        # ``link()`` is synchronous and runs on the event loop: it must never touch the DB, so
        # the proxy host name is cached (loaded at start, refreshed on settings changes/create).
        self._host_cache: str | None = None

    async def load_hostname(self) -> str:
        host = str(await self._db.run(repo.get_setting, "proxy_hostname", "") or "")
        self._host_cache = host
        return host

    # ------------------------------------------------------------------ plumbing

    def _now(self) -> datetime:
        return self._pipeline.now()

    async def _run(
        self,
        mutation: Callable[[sqlite3.Connection], list[int]],
        *,
        reason: str,
        actor: Actor,
        with_links: bool = False,
    ) -> OperationResult:
        outcome = await self._pipeline.run_operation(mutation, reason=reason, actor=actor)
        if not outcome.ok:
            return OperationResult(ok=False, error=outcome.error, apply_run_id=outcome.apply_run_id)
        ids = tuple(outcome.value or ())
        links: dict[int, str] = {}
        if with_links and ids:
            await self.load_hostname()
            users = await self._db.run(repo.users_by_ids, list(ids))
            links = {u.id: self.link(u) for u in users}
        return OperationResult(
            ok=True, user_ids=ids, apply_run_id=outcome.apply_run_id, links=links
        )

    @staticmethod
    def _load(conn: sqlite3.Connection, ids: Sequence[int]) -> list[UserRecord]:
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise OperationRejected("Не выбрано ни одного пользователя")
        users = repo.users_by_ids(conn, unique)
        found = {u.id for u in users}
        missing = [i for i in unique if i not in found]
        if missing:
            raise OperationRejected(f"Пользователи не найдены: {_ids_text(missing)}")
        return users

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor: Actor,
        action: str,
        user_id: int,
        details: str = "",
    ) -> None:
        repo.add_audit(conn, self._now(), actor, action, f"user:{user_id}", details)

    # ------------------------------------------------------------------ create

    async def create(self, users: list[NewUser], actor: Actor) -> OperationResult:
        if not users:
            return OperationResult(ok=False, error="Не указано ни одного пользователя")
        if not await self.load_hostname():
            return OperationResult(ok=False, error="Не задано имя хоста прокси (proxy_hostname)")

        def mutation(conn: sqlite3.Connection) -> list[int]:
            now = self._now()
            cfg = read_settings(conn)
            names: list[str] = []
            seen_tg: set[int] = set()
            for nu in users:
                names.append(_check_name(nu.name))
                _check_comment(nu.comment)
                if nu.tg_id is not None:
                    _check_tg_id(nu.tg_id)
                    if nu.tg_id in seen_tg:
                        raise OperationRejected(f"Telegram ID {nu.tg_id} указан дважды")
                    seen_tg.add(nu.tg_id)
                    if repo.get_user_by_tg_id(conn, nu.tg_id) is not None:
                        raise OperationRejected(f"Telegram ID {nu.tg_id} уже привязан")
                if nu.expires_at is not None and nu.expires_at <= now:
                    raise OperationRejected("Срок действия должен быть в будущем")
            dup = [n for n in dict.fromkeys(names) if names.count(n) > 1]
            if dup:
                raise OperationRejected(f"Имя «{dup[0]}» указано дважды")
            for n in names:
                if conn.execute("SELECT 1 FROM users WHERE name = ?", (n,)).fetchone():
                    raise OperationRejected(f"Имя «{n}» уже занято")
            pools = repo.list_pools(conn)
            existing = repo.all_users(conn)
            alloc = allocate_pools(pools, existing, len(users), cfg.secrets_per_process)
            for pool in alloc.new_pools:
                repo.insert_pool(conn, pool, now)
            ips = allocate_addresses(repo.used_loopback_ips(conn), len(users))
            bases = {base_secret(u.secret) for u in existing}
            sentinel = repo.get_setting(conn, "sentinel_secret")
            if sentinel:
                bases.add(base_secret(sentinel))
            default_exp: datetime | None = None
            ids: list[int] = []
            for nu, name, pool_id, ip in zip(users, names, alloc.pool_ids, ips, strict=True):
                secret = generate_secret()
                while secret in bases:  # pragma: no cover - 2^-128
                    secret = generate_secret()
                bases.add(secret)
                if nu.expires_at is not None:
                    expiry = nu.expires_at
                else:
                    default_exp = default_exp or default_term_expiry(cfg, now)
                    expiry = default_exp
                uid = repo.insert_user(
                    conn,
                    name=name,
                    secret=secret,
                    status=UserStatus.ACTIVE,
                    pool_id=pool_id,
                    loopback_ip=ip,
                    created_at=now,
                    carrier_mode=nu.carrier_mode,
                    expires_at=expiry,
                    comment=nu.comment,
                    tg_id=nu.tg_id,
                )
                ids.append(uid)
                self._audit(conn, actor, "user.create", uid, f"pool={pool_id}")
            return ids

        return await self._run(mutation, reason="create", actor=actor, with_links=True)

    # ------------------------------------------------------------------ status / expiry

    async def set_status(self, ids: list[int], enabled: bool, actor: Actor) -> OperationResult:
        def mutation(conn: sqlite3.Connection) -> list[int]:
            now = self._now()
            targets = self._load(conn, ids)
            if enabled:
                stale = [
                    u.id
                    for u in targets
                    if u.status is not UserStatus.ACTIVE
                    and u.expires_at is not None
                    and u.expires_at <= now
                ]
                if stale:
                    raise OperationRejected(
                        f"Срок действия истёк (ID {_ids_text(stale)}): сначала продлите"
                    )
            for u in targets:
                if enabled and u.status is not UserStatus.ACTIVE:
                    repo.update_user(conn, u.id, status=UserStatus.ACTIVE, disabled_reason=None)
                elif not enabled and u.status is not UserStatus.DISABLED:
                    repo.update_user(
                        conn, u.id, status=UserStatus.DISABLED, disabled_reason="manual"
                    )
                else:
                    continue
                self._audit(conn, actor, "user.enable" if enabled else "user.disable", u.id)
            return [u.id for u in targets]

        return await self._run(mutation, reason="enable" if enabled else "disable", actor=actor)

    async def set_expiry(
        self, ids: list[int], expires_at: datetime | None, actor: Actor
    ) -> OperationResult:
        def mutation(conn: sqlite3.Connection) -> list[int]:
            now = self._now()
            targets = self._load(conn, ids)
            for u in targets:
                status = u.status
                if status is UserStatus.EXPIRED and (expires_at is None or expires_at > now):
                    status = UserStatus.ACTIVE
                elif status is UserStatus.ACTIVE and expires_at is not None and expires_at <= now:
                    status = UserStatus.EXPIRED
                fields: dict[str, object] = {"expires_at": expires_at}
                if status is not u.status:
                    fields["status"] = status
                    fields["disabled_reason"] = "expired" if status is UserStatus.EXPIRED else None
                repo.update_user(conn, u.id, **fields)
                self._audit(
                    conn,
                    actor,
                    "user.set_expiry",
                    u.id,
                    "none" if expires_at is None else expires_at.strftime("%Y-%m-%d %H:%M"),
                )
            return [u.id for u in targets]

        return await self._run(mutation, reason="set_expiry", actor=actor)

    async def extend(self, ids: list[int], days: int, actor: Actor) -> OperationResult:
        if not 1 <= days <= MAX_EXTEND_DAYS:
            return OperationResult(ok=False, error=f"Продление: от 1 до {MAX_EXTEND_DAYS} дней")

        def mutation(conn: sqlite3.Connection) -> list[int]:
            now = self._now()
            targets = self._load(conn, ids)
            for u in targets:
                new_exp = extend_expiry(u.expires_at, now, days=days)
                new_status = status_after_extend(u.status)
                fields: dict[str, object] = {"expires_at": new_exp}
                if new_status is not u.status:
                    fields["status"] = new_status
                    fields["disabled_reason"] = None
                repo.update_user(conn, u.id, **fields)
                self._audit(conn, actor, "user.extend", u.id, f"days={days}")
            return [u.id for u in targets]

        return await self._run(mutation, reason="extend", actor=actor)

    async def expire_due(self, now: datetime) -> OperationResult:
        due = await self._db.run(lambda c: due_for_expiry(repo.all_users(c), now))
        if not due:
            return OperationResult(ok=True)

        def mutation(conn: sqlite3.Connection) -> list[int]:
            current = due_for_expiry(repo.all_users(conn), now)
            for u in current:
                repo.update_user(conn, u.id, status=UserStatus.EXPIRED, disabled_reason="expired")
                self._audit(conn, "system", "user.expire", u.id)
            return [u.id for u in current]

        return await self._run(mutation, reason="expire", actor="system")

    # ------------------------------------------------------------------ secrets / modes

    async def reissue_secret(self, user_id: int, actor: Actor) -> OperationResult:
        def mutation(conn: sqlite3.Connection) -> list[int]:
            (user,) = self._load(conn, [user_id])
            bases = {base_secret(u.secret) for u in repo.all_users(conn)}
            sentinel = repo.get_setting(conn, "sentinel_secret")
            if sentinel:
                bases.add(base_secret(sentinel))
            secret = generate_secret()
            while secret in bases:  # pragma: no cover
                secret = generate_secret()
            repo.update_user(conn, user.id, secret=secret)
            self._audit(conn, actor, "user.reissue_secret", user.id)
            return [user.id]

        return await self._run(mutation, reason="reissue", actor=actor, with_links=True)

    async def set_carrier_mode(
        self, ids: list[int], mode: CarrierMode | None, actor: Actor
    ) -> OperationResult:
        def mutation(conn: sqlite3.Connection) -> list[int]:
            targets = self._load(conn, ids)
            for u in targets:
                repo.update_user(conn, u.id, carrier_mode=mode)
                self._audit(
                    conn, actor, "user.set_carrier_mode", u.id, "default" if mode is None else mode
                )
            return [u.id for u in targets]

        return await self._run(mutation, reason="carrier_mode", actor=actor)

    async def delete(self, ids: list[int], actor: Actor) -> OperationResult:
        def mutation(conn: sqlite3.Connection) -> list[int]:
            targets = self._load(conn, ids)
            for u in targets:
                self._audit(conn, actor, "user.delete", u.id, f"pool={u.pool_id}")
            repo.delete_users(conn, [u.id for u in targets])
            return [u.id for u in targets]

        return await self._run(mutation, reason="delete", actor=actor)

    # ------------------------------------------------------------------ DB-only edits

    async def update_meta(
        self,
        user_id: int,
        actor: Actor,
        *,
        name: str | None = None,
        comment: str | None = None,
        tg_id: int | None = None,
        tg_username: str | None = None,
    ) -> None:
        """Name / comment / Telegram data: DB only, never touches the proxy (no apply)."""

        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                user = repo.get_user(conn, user_id)
                if user is None:
                    raise UserServiceError("Пользователь не найден")
                fields: dict[str, object] = {}
                changed: list[str] = []
                if name is not None:
                    value = _check_name(name)
                    clash = conn.execute(
                        "SELECT id FROM users WHERE name = ? AND id != ?", (value, user_id)
                    ).fetchone()
                    if clash:
                        raise UserServiceError(f"Имя «{value}» уже занято")
                    fields["name"] = value
                    changed.append("name")
                if comment is not None:
                    fields["comment"] = _check_comment(comment)
                    changed.append("comment")
                if tg_id is not None:
                    _check_tg_id(tg_id)
                    other = repo.get_user_by_tg_id(conn, tg_id)
                    if other is not None and other.id != user_id:
                        raise UserServiceError(f"Telegram ID {tg_id} уже привязан")
                    fields["tg_id"] = tg_id
                    changed.append("tg_id")
                if tg_username is not None:
                    fields["tg_username"] = tg_username.strip().lstrip("@") or None
                    changed.append("tg_username")
                if not fields:
                    return
                repo.update_user(conn, user_id, **fields)
                self._audit(conn, actor, "user.update_meta", user_id, ",".join(changed))

        await self._pipeline.db_write(work)

    # ------------------------------------------------------------------ queries / links

    async def list(self, query: UserListQuery) -> UserPage:
        rows, total = await self._db.run(repo.list_users, query, self._now())
        return UserPage(
            rows=tuple(
                UserRow(r.user, r.online, r.bytes_up, r.bytes_down, r.first_seen_at, r.last_seen_at)
                for r in rows
            ),
            total=total,
        )

    async def get(self, user_id: int) -> UserRecord | None:
        return await self._db.run(repo.get_user, user_id)

    def _host(self) -> str:
        host = self._host_cache
        if not host:
            raise UserServiceError("Не задано имя хоста прокси (proxy_hostname)")
        return host

    def link(self, user: UserRecord) -> str:
        return https_link(self._host(), user.secret)

    def tg_link(self, user: UserRecord) -> str:
        return tg_link(self._host(), user.secret)
