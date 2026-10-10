"""Service-layer contract used by web/bot/scheduler. CONTRACT: change only via orchestrator.

Every mutating method that touches proxy-side state performs exactly ONE apply and returns only
after it finished (relay restarted, /readyz OK) or failed (rolled back). Links in results are
filled only on success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from tgpanel.domain.models import CarrierMode, UserRecord
from tgpanel.domain.queries import Period, SortField, UserFilter, UserListQuery

__all__ = [
    "Actor",
    "NewUser",
    "OperationResult",
    "Period",
    "SortField",
    "UserFilter",
    "UserListQuery",
    "UserPage",
    "UserRow",
    "UserService",
]

Actor = str  # "web:<login>" | "bot:<tg_id>" | "system"


@dataclass(frozen=True, slots=True)
class NewUser:
    name: str
    tg_id: int | None = None
    comment: str = ""
    expires_at: datetime | None = None
    carrier_mode: CarrierMode | None = None
    display_name: str = ""  # free-form label ("Имя"); `name` is the technical profile name


@dataclass(frozen=True, slots=True)
class OperationResult:
    ok: bool
    user_ids: tuple[int, ...] = ()
    error: str | None = None  # human-readable (Russian), no secrets
    apply_run_id: int | None = None
    links: dict[int, str] = field(default_factory=dict)  # user_id -> https link, success only


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
    async def extend(self, ids: list[int], days: int, actor: Actor) -> OperationResult:
        """Disabled users stay disabled on extend: deliberate deviation from PLAN 7."""
        ...

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
        display_name: str | None = None,
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
