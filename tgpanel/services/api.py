"""Service-layer contract used by web/bot/scheduler. CONTRACT: change only via orchestrator.

Every mutating method that touches proxy-side state performs exactly ONE apply and returns only
after it finished (relay restarted, /readyz OK) or failed (rolled back). Links in results are
filled only on success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from tgpanel.domain.models import CarrierMode, UserRecord, UserStatus

Actor = str  # "web:<login>" | "bot:<tg_id>" | "system"


@dataclass(frozen=True, slots=True)
class NewUser:
    name: str
    tg_id: int | None = None
    comment: str = ""
    expires_at: datetime | None = None
    carrier_mode: CarrierMode | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    ok: bool
    user_ids: tuple[int, ...] = ()
    error: str | None = None  # human-readable (Russian), no secrets
    apply_run_id: int | None = None
    links: dict[int, str] = field(default_factory=dict)  # user_id -> https link, success only


SortField = Literal[
    "id",
    "name",
    "comment",
    "tg_id",
    "tg_username",
    "status",
    "online",
    "created_at",
    "expires_at",
    "first_seen_at",
    "last_seen_at",
    "traffic",
    "pool_id",
    "bot_started",
]
Period = Literal["24h", "7d", "30d", "all"]


@dataclass(frozen=True, slots=True)
class UserFilter:
    query: str | None = None
    statuses: tuple[UserStatus, ...] = ()
    online: bool | None = None
    imported: bool | None = None
    has_tg_id: bool | None = None
    bot_started: bool | None = None
    expires_within_days: int | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    last_seen_from: datetime | None = None
    last_seen_to: datetime | None = None
    traffic_min: int | None = None
    traffic_max: int | None = None
    comment_contains: str | None = None


@dataclass(frozen=True, slots=True)
class UserListQuery:
    filter: UserFilter = UserFilter()
    sort: SortField = "id"
    descending: bool = False
    period: Period = "30d"
    page: int = 1
    per_page: Literal[50, 100, 200] = 50


@dataclass(frozen=True, slots=True)
class UserRow:
    user: UserRecord
    online: bool
    bytes_up: int
    bytes_down: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class UserPage:
    rows: tuple[UserRow, ...]
    total: int


class UserService(Protocol):
    async def create(self, users: list[NewUser], actor: Actor) -> OperationResult: ...
    async def set_status(self, ids: list[int], enabled: bool, actor: Actor) -> OperationResult: ...
    async def set_expiry(
        self, ids: list[int], expires_at: datetime | None, actor: Actor
    ) -> OperationResult: ...
    async def extend(self, ids: list[int], days: int, actor: Actor) -> OperationResult: ...
    async def reissue_secret(self, user_id: int, actor: Actor) -> OperationResult: ...
    async def set_carrier_mode(
        self, ids: list[int], mode: CarrierMode | None, actor: Actor
    ) -> OperationResult: ...
    async def delete(self, ids: list[int], actor: Actor) -> OperationResult: ...
    async def expire_due(self, now: datetime) -> OperationResult: ...
    # DB-only edits (no apply)
    async def update_meta(
        self,
        user_id: int,
        actor: Actor,
        *,
        name: str | None = None,
        comment: str | None = None,
        tg_id: int | None = None,
        tg_username: str | None = None,
    ) -> None: ...
    async def list(self, query: UserListQuery) -> UserPage: ...
    async def get(self, user_id: int) -> UserRecord | None: ...
    def link(self, user: UserRecord) -> str:
        """https://t.me/webproxy?server=<host>&secret=<secret>"""
        ...

    def tg_link(self, user: UserRecord) -> str: ...
