"""Expiry computation and selection (PLAN 7, 6.1). Pure; ``now`` is always explicit."""

from __future__ import annotations

import calendar
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from enum import StrEnum

from tgpanel.domain.models import UserRecord, UserStatus

REMINDER_DAYS_BEFORE = 3


class Term(StrEnum):
    DAY = "1d"
    MONTH = "1m"
    YEAR = "1y"
    DATE = "date"


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError("datetime must be timezone-aware UTC")


def add_months(dt: datetime, months: int) -> datetime:
    """Calendar-correct month addition (day clamped to the target month length)."""
    _require_aware(dt)
    total = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def default_expiry(term: Term, now: datetime, explicit: datetime | None = None) -> datetime:
    _require_aware(now)
    if term is Term.DAY:
        return now + timedelta(days=1)
    if term is Term.MONTH:
        return add_months(now, 1)
    if term is Term.YEAR:
        return add_months(now, 12)
    if explicit is None:
        raise ValueError("explicit date required for Term.DATE")
    _require_aware(explicit)
    return explicit


def due_for_expiry(users: Iterable[UserRecord], now: datetime) -> list[UserRecord]:
    """Active users whose expires_at has passed (expires_at <= now)."""
    return [
        u
        for u in users
        if u.status is UserStatus.ACTIVE and u.expires_at is not None and u.expires_at <= now
    ]


def extend_expiry(
    expires_at: datetime | None,
    now: datetime,
    *,
    days: int = 0,
    months: int = 0,
) -> datetime:
    """Extend from max(now, expires_at); no current expiry counts as ``now``."""
    base = now if expires_at is None else max(now, expires_at)
    return add_months(base, months) + timedelta(days=days)


def status_after_extend(status: UserStatus) -> UserStatus:
    """Extension revives EXPIRED users; manually DISABLED users stay disabled."""
    return UserStatus.ACTIVE if status is UserStatus.EXPIRED else status


def select_reminders(
    items: Iterable[tuple[UserRecord, datetime]],
    now: datetime,
    *,
    days_before: int = REMINDER_DAYS_BEFORE,
    already_reminded: Collection[int] = (),
) -> list[UserRecord]:
    """Users to remind: active, ``days_before`` or less left, not yet expired.

    ``items`` pairs each user with its term start (created_at). Skipped when the total
    term (expires_at - start) is <= 1 day.
    """
    out: list[UserRecord] = []
    for user, start in items:
        exp = user.expires_at
        if user.status is not UserStatus.ACTIVE or exp is None or user.id in already_reminded:
            continue
        if exp - start <= timedelta(days=1):
            continue
        if exp > now and exp - now <= timedelta(days=days_before):
            out.append(user)
    return out
