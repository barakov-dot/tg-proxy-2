"""User list query types shared by services and the DB layer (pure, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from tgpanel.domain.models import UserStatus

PER_PAGE_CHOICES = (50, 100, 200)

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
