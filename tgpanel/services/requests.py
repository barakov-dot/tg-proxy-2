"""Access requests from the Telegram bot (PLAN 6.1).

One pending request per Telegram ID (partial unique index), a rate limit, a blacklist
(setting ``bot_blacklist``: ids separated by commas/spaces), and two issuance modes:
``open`` (the profile is created at once) and ``approval`` (an admin decides).
Approving creates the user through ``UserService.create`` - ONE apply - and the link is
returned only after that apply succeeded. Approving twice is harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import read_settings
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.db.times import to_db
from tgpanel.domain.expiry import Term, default_expiry
from tgpanel.domain.models import UserRecord
from tgpanel.services.api import NewUser, UserService

KEY_BLACKLIST = "bot_blacklist"
DEFAULT_OPEN_LIMIT = 6
RATE_WINDOW = timedelta(hours=1)
RATE_MAX_REQUESTS = 3
MAX_NAME = 100


class RequestKind(StrEnum):
    CREATED = "created"  # approval mode: pending, admins must be told
    ALREADY_PENDING = "already_pending"
    BLACKLISTED = "blacklisted"
    RATE_LIMITED = "rate_limited"
    HAS_ACCESS = "has_access"
    ISSUED = "issued"  # open mode (or approve): user exists, ``link`` is set
    FAILED = "failed"  # apply failed: no link, request stays pending
    ALREADY_DECIDED = "already_decided"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    kind: RequestKind
    request: repo.AccessRequest | None = None
    user: UserRecord | None = None
    link: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StartInfo:
    user: UserRecord | None
    first_start: bool  # the bot was just linked to an existing (e.g. imported) user


_ASCII_ID = re.compile(r"[0-9]{1,16}")


def parse_ids(raw: str) -> set[int]:
    """Telegram ids from a free-form list; anything that is not a plain ASCII number is skipped."""
    out: set[int] = set()
    for token in re.split(r"[\s,;]+", raw.strip()):
        if _ASCII_ID.fullmatch(token):
            out.add(int(token))
    return out


def display_name(full_name: str, username: str | None, tg_id: int) -> str:
    name = " ".join(full_name.split())[:MAX_NAME].strip()
    if not name and username:
        name = "@" + username.lstrip("@")
    return name or f"tg{tg_id}"


def _bind_user(conn: sqlite3.Connection, user_id: int, username: str | None) -> None:
    fields: dict[str, object] = {"bot_started": True, "can_message": True}
    if username:
        fields["tg_username"] = username.lstrip("@")
    repo.update_user(conn, user_id, **fields)


class RequestService:
    def __init__(
        self,
        pipeline: ApplyPipeline,
        db: Database,
        users: UserService,
        *,
        rate_max: int = RATE_MAX_REQUESTS,
        rate_window: timedelta = RATE_WINDOW,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pipeline = pipeline
        self._db = db
        self._users = users
        self._rate_max = rate_max
        self._rate_window = rate_window
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._open_inflight = 0
        self._quota_lock = asyncio.Lock()
        self._sleep = sleep
        # open mode: requests wait here for the batch window and are issued by ONE apply
        self._batch: list[tuple[int, asyncio.Future[RequestOutcome]]] = []
        self._batch_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ reads

    async def issuance_mode(self) -> str:
        return (await self._db.run(read_settings)).issuance_mode

    @contextlib.asynccontextmanager
    async def _locked(self, request_id: int) -> AsyncIterator[None]:
        lock, users = self._locks.get(request_id, (asyncio.Lock(), 0))
        self._locks[request_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[request_id]
            if users <= 1:
                del self._locks[request_id]
            else:
                self._locks[request_id] = (lock, users - 1)

    async def blacklist_ids(self) -> list[int]:
        try:
            raw = await self._db.run(repo.get_setting, KEY_BLACKLIST, "")
            return sorted(parse_ids(str(raw or "")))
        except Exception:
            return []

    async def is_blacklisted(self, tg_id: int) -> bool:
        return tg_id in await self.blacklist_ids()

    async def blacklist_edit(self, tg_id: int, add: bool, actor: str) -> list[int]:
        """Add or remove one id of the blacklist (written under the pipeline lock)."""
        if not 0 < tg_id <= 2**53:
            raise OperationRejected("Некорректный Telegram ID")

        def work(conn: sqlite3.Connection) -> list[int]:
            with transaction(conn):
                ids = parse_ids(repo.get_setting(conn, KEY_BLACKLIST, "") or "")
                if add:
                    ids.add(tg_id)
                else:
                    ids.discard(tg_id)
                repo.set_setting(conn, KEY_BLACKLIST, ",".join(str(i) for i in sorted(ids)))
                repo.add_audit(
                    conn,
                    self._pipeline.now(),
                    actor,
                    "blacklist.add" if add else "blacklist.remove",
                    f"tg:{tg_id}",
                )
                return sorted(ids)

        return await self._pipeline.db_write(work)

    async def _reserve_open(self) -> bool:
        async with self._quota_lock:
            if not await self._open_quota_left():
                return False
            self._open_inflight += 1
            return True

    async def _open_quota_left(self) -> bool:
        """Global limit of profiles issued without approval per hour."""
        try:
            limit = (await self._db.run(read_settings)).open_mode_max_per_hour
        except Exception:
            limit = DEFAULT_OPEN_LIMIT
        since = to_db(self._pipeline.now() - timedelta(hours=1))

        def count(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM access_requests WHERE status = 'approved'"
                " AND decided_by = 'system' AND decided_at >= ?",
                (since,),
            ).fetchone()
            return int(row[0])

        return await self._db.run(count) + self._open_inflight < limit

    async def get(self, request_id: int) -> repo.AccessRequest | None:
        return await self._db.run(repo.get_access_request, request_id)

    async def list_pending(self) -> list[repo.AccessRequest]:
        return await self._db.run(repo.list_access_requests, "pending")

    # ------------------------------------------------------------------ bot /start

    async def register_start(self, tg_id: int, username: str | None) -> StartInfo:
        """Bind the chat to an existing user (imported or created by hand) at /start."""
        user = await self._db.run(repo.get_user_by_tg_id, tg_id)
        if user is None:
            return StartInfo(None, False)
        extra = await self._db.run(repo.get_user_extra, user.id)
        first = extra is None or not extra.bot_started
        wanted = (username or "").lstrip("@") or None
        if (
            extra is None
            or not extra.bot_started
            or not extra.can_message
            or (wanted is not None and wanted != extra.tg_username)
        ):

            def work(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    _bind_user(conn, user.id, username)

            await self._pipeline.db_write(work)
        return StartInfo(user, first)

    # ------------------------------------------------------------------ submit

    async def submit(
        self,
        tg_id: int,
        username: str | None,
        full_name: str,
        *,
        on_preparing: Callable[[], Awaitable[None]] | None = None,
    ) -> RequestOutcome:
        reserved = [False]  # one slot of the hourly open-mode quota, released when done
        try:
            return await self._submit(tg_id, username, full_name, on_preparing, reserved)
        finally:
            if reserved[0]:
                self._open_inflight -= 1

    async def _submit(
        self,
        tg_id: int,
        username: str | None,
        full_name: str,
        on_preparing: Callable[[], Awaitable[None]] | None,
        reserved: list[bool],
    ) -> RequestOutcome:
        if await self.is_blacklisted(tg_id):
            return RequestOutcome(RequestKind.BLACKLISTED)
        user = await self._db.run(repo.get_user_by_tg_id, tg_id)
        if user is not None:
            return RequestOutcome(RequestKind.HAS_ACCESS, user=user)
        open_mode = await self.issuance_mode() == "open"
        if open_mode:
            # hourly limit of unattended issuance reached: admins decide instead
            reserved[0] = open_mode = await self._reserve_open()
        pending = await self._db.run(repo.pending_request_for, tg_id)
        if pending is not None:
            if not open_mode:
                return RequestOutcome(RequestKind.ALREADY_PENDING, request=pending)
            # open mode: an earlier issuance failed; pressing the button again retries it
            if on_preparing is not None:
                await on_preparing()
            return await self._approve_open(pending.id)
        if await self._rate_limited(tg_id):
            return RequestOutcome(RequestKind.RATE_LIMITED)

        def create(conn: sqlite3.Connection) -> int | None:
            with transaction(conn):
                try:
                    rid = repo.create_access_request(
                        conn,
                        tg_id,
                        (username or "").lstrip("@") or None,
                        full_name[:200],
                        self._pipeline.now(),
                    )
                except sqlite3.IntegrityError:
                    return None
                repo.add_audit(
                    conn, self._pipeline.now(), f"bot:{tg_id}", "request.create", f"request:{rid}"
                )
                return rid

        rid = await self._pipeline.db_write(create)
        if rid is None:
            return RequestOutcome(
                RequestKind.ALREADY_PENDING,
                request=await self._db.run(repo.pending_request_for, tg_id),
            )
        if not open_mode:
            return RequestOutcome(
                RequestKind.CREATED, request=await self._db.run(repo.get_access_request, rid)
            )
        if on_preparing is not None:
            await on_preparing()  # "готовим…" goes out at once; the link follows the batch apply
        return await self._approve_open(rid)

    async def _rate_limited(self, tg_id: int) -> bool:
        since = to_db(self._pipeline.now() - self._rate_window)

        def count(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM access_requests WHERE tg_id = ? AND created_at >= ?",
                (tg_id, since),
            ).fetchone()
            return int(row[0])

        return await self._db.run(count) >= self._rate_max

    # ------------------------------------------------------------------ open-mode batching

    async def _batch_window(self) -> int:
        try:
            return (await self._db.run(read_settings)).open_mode_batch_window_s
        except Exception:
            return 0

    async def _approve_open(self, request_id: int) -> RequestOutcome:
        """Open-mode issuance: requests arriving within the batch window share ONE apply."""
        window = await self._batch_window()
        if window <= 0:
            return await self._approve(request_id, None, "system")
        future: asyncio.Future[RequestOutcome] = asyncio.get_running_loop().create_future()
        self._batch.append((request_id, future))
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._flush_after(window))
        return await future

    async def _flush_after(self, window: int) -> None:
        await self._sleep(window)
        while self._batch:
            items, self._batch = self._batch, []
            try:
                outcomes = await self._issue_batch([rid for rid, _ in items])
            except Exception:
                outcomes = [
                    RequestOutcome(RequestKind.FAILED, error="Не удалось создать доступ")
                    for _ in items
                ]
            for (_, future), outcome in zip(items, outcomes, strict=True):
                if not future.done():
                    future.set_result(outcome)

    async def _issue_batch(self, request_ids: list[int]) -> list[RequestOutcome]:
        """Create all pending requests of the batch with one ``UserService.create`` (one apply)."""
        async with contextlib.AsyncExitStack() as stack:
            for rid in sorted(set(request_ids)):  # fixed order: no lock-order deadlocks
                await stack.enter_async_context(self._locked(rid))
            outcomes: dict[int, RequestOutcome] = {}
            todo: list[tuple[int, repo.AccessRequest, NewUser]] = []
            used: set[str] = set()
            for index, rid in enumerate(request_ids):
                req = await self._db.run(repo.get_access_request, rid)
                if req is None:
                    outcomes[index] = RequestOutcome(RequestKind.NOT_FOUND)
                elif req.status != "pending":
                    outcomes[index] = RequestOutcome(RequestKind.ALREADY_DECIDED, request=req)
                else:
                    existing = await self._db.run(repo.get_user_by_tg_id, req.tg_id)
                    if existing is not None:
                        await self._finalize(req, existing.id, "system", "approved")
                        outcomes[index] = RequestOutcome(
                            RequestKind.ISSUED,
                            request=req,
                            user=existing,
                            link=self._safe_link(existing),
                        )
                        continue
                    name = await self._free_name(
                        display_name(req.full_name, req.tg_username, req.tg_id), req
                    )
                    suffix = f" ({req.tg_id})"
                    if name in used:
                        name = name[: MAX_NAME - len(suffix)] + suffix
                    used.add(name)
                    todo.append(
                        (
                            index,
                            req,
                            NewUser(name=name, tg_id=req.tg_id, comment="заявка из бота"),
                        )
                    )
            if todo:
                result = await self._users.create([nu for _, _, nu in todo], "system")
                if not result.ok or len(result.user_ids) != len(todo):
                    for index, req, _ in todo:
                        outcomes[index] = RequestOutcome(
                            RequestKind.FAILED,
                            request=req,
                            error=result.error or "Не удалось создать доступ",
                        )
                else:
                    for (index, req, _), uid in zip(todo, result.user_ids, strict=True):
                        await self._finalize(req, uid, "system", "approved")
                        outcomes[index] = RequestOutcome(
                            RequestKind.ISSUED,
                            request=req,
                            user=await self._users.get(uid),
                            link=result.links.get(uid),
                        )
            return [outcomes[i] for i in range(len(request_ids))]

    # ------------------------------------------------------------------ decisions

    async def approve(self, request_id: int, term: Term | None, actor: str) -> RequestOutcome:
        return await self._approve(request_id, term, actor)

    async def _approve(self, request_id: int, term: Term | None, actor: str) -> RequestOutcome:
        async with self._locked(request_id):
            return await self._approve_locked(request_id, term, actor)

    async def _approve_locked(
        self, request_id: int, term: Term | None, actor: str
    ) -> RequestOutcome:
        req = await self._db.run(repo.get_access_request, request_id)
        if req is None:
            return RequestOutcome(RequestKind.NOT_FOUND)
        if req.status != "pending":
            return RequestOutcome(RequestKind.ALREADY_DECIDED, request=req)
        existing = await self._db.run(repo.get_user_by_tg_id, req.tg_id)
        if existing is not None:
            await self._finalize(req, existing.id, actor, "approved")
            return RequestOutcome(
                RequestKind.ISSUED, request=req, user=existing, link=self._safe_link(existing)
            )
        name = await self._free_name(display_name(req.full_name, req.tg_username, req.tg_id), req)
        expires: datetime | None = None
        if term is not None:
            expires = default_expiry(term, self._pipeline.now())
        result = await self._users.create(
            [NewUser(name=name, tg_id=req.tg_id, comment="заявка из бота", expires_at=expires)],
            actor,
        )
        if not result.ok or not result.user_ids:
            return RequestOutcome(
                RequestKind.FAILED, request=req, error=result.error or "Не удалось создать доступ"
            )
        uid = result.user_ids[0]
        await self._finalize(req, uid, actor, "approved")
        user = await self._users.get(uid)
        return RequestOutcome(
            RequestKind.ISSUED, request=req, user=user, link=result.links.get(uid)
        )

    def _safe_link(self, user: UserRecord) -> str | None:
        try:
            return self._users.link(user)
        except Exception:
            return None

    async def _free_name(self, base: str, req: repo.AccessRequest) -> str:
        def taken(conn: sqlite3.Connection, name: str) -> bool:
            return (
                conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone() is not None
            )

        if not await self._db.run(taken, base):
            return base
        suffix = f" ({req.tg_id})"
        return base[: MAX_NAME - len(suffix)] + suffix

    async def _finalize(
        self, req: repo.AccessRequest, user_id: int, actor: str, status: str
    ) -> None:
        def work(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                now = self._pipeline.now()
                _bind_user(conn, user_id, req.tg_username)
                repo.decide_access_request(conn, req.id, status, actor, now)
                repo.add_audit(conn, now, actor, f"request.{status}", f"request:{req.id}")

        await self._pipeline.db_write(work)

    async def reject(self, request_id: int, actor: str) -> RequestOutcome:
        async with self._locked(request_id):
            req = await self._db.run(repo.get_access_request, request_id)
            if req is None:
                return RequestOutcome(RequestKind.NOT_FOUND)
            if req.status != "pending":
                return RequestOutcome(RequestKind.ALREADY_DECIDED, request=req)

            def work(conn: sqlite3.Connection) -> None:
                with transaction(conn):
                    now = self._pipeline.now()
                    repo.decide_access_request(conn, request_id, "rejected", actor, now)
                    repo.add_audit(conn, now, actor, "request.rejected", f"request:{request_id}")

            await self._pipeline.db_write(work)
            return RequestOutcome(
                RequestKind.REJECTED,
                request=await self._db.run(repo.get_access_request, request_id),
            )
