from datetime import UTC, datetime, timedelta

import pytest

from tests.domain.helpers import NOW, make_user
from tgpanel.domain.expiry import (
    Term,
    add_months,
    default_expiry,
    due_for_expiry,
    extend_expiry,
    select_reminders,
    status_after_extend,
)
from tgpanel.domain.models import UserStatus


def dt(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def test_terms() -> None:
    assert default_expiry(Term.DAY, NOW) == NOW + timedelta(days=1)
    assert default_expiry(Term.MONTH, NOW) == dt(2026, 4, 10, 12)
    assert default_expiry(Term.YEAR, NOW) == dt(2027, 3, 10, 12)
    when = dt(2026, 12, 31)
    assert default_expiry(Term.DATE, NOW, when) == when
    with pytest.raises(ValueError):
        default_expiry(Term.DATE, NOW)


def test_month_clamping_and_leap_years() -> None:
    assert add_months(dt(2026, 1, 31), 1) == dt(2026, 2, 28)
    assert add_months(dt(2024, 1, 31), 1) == dt(2024, 2, 29)
    assert add_months(dt(2024, 2, 29), 12) == dt(2025, 2, 28)
    assert add_months(dt(2026, 12, 15), 1) == dt(2027, 1, 15)
    assert add_months(dt(2026, 11, 30), 3) == dt(2027, 2, 28)
    assert add_months(dt(2026, 3, 31), -1) == dt(2026, 2, 28)


def test_naive_rejected() -> None:
    with pytest.raises(ValueError):
        default_expiry(Term.DAY, datetime(2026, 1, 1))


def test_due_for_expiry() -> None:
    users = [
        make_user(1, expires_at=NOW - timedelta(seconds=1)),
        make_user(2, expires_at=NOW),
        make_user(3, expires_at=NOW + timedelta(seconds=1)),
        make_user(4, expires_at=None),
        make_user(5, status=UserStatus.DISABLED, expires_at=NOW - timedelta(days=1)),
        make_user(6, status=UserStatus.EXPIRED, expires_at=NOW - timedelta(days=1)),
    ]
    assert [u.id for u in due_for_expiry(users, NOW)] == [1, 2]


def test_extend_from_max_of_now_and_expiry() -> None:
    future = NOW + timedelta(days=5)
    assert extend_expiry(future, NOW, days=10) == NOW + timedelta(days=15)
    past = NOW - timedelta(days=5)
    assert extend_expiry(past, NOW, days=10) == NOW + timedelta(days=10)
    assert extend_expiry(None, NOW, days=1) == NOW + timedelta(days=1)
    assert extend_expiry(future, NOW, months=1) == add_months(future, 1)


def test_status_after_extend() -> None:
    assert status_after_extend(UserStatus.EXPIRED) is UserStatus.ACTIVE
    assert status_after_extend(UserStatus.ACTIVE) is UserStatus.ACTIVE
    assert status_after_extend(UserStatus.DISABLED) is UserStatus.DISABLED


def test_reminders() -> None:
    created = NOW - timedelta(days=30)
    soon = make_user(1, expires_at=NOW + timedelta(days=2))
    edge = make_user(2, expires_at=NOW + timedelta(days=3))
    far = make_user(3, expires_at=NOW + timedelta(days=4))
    past = make_user(4, expires_at=NOW - timedelta(hours=1))
    short = make_user(5, expires_at=NOW + timedelta(hours=5))
    disabled = make_user(6, status=UserStatus.DISABLED, expires_at=NOW + timedelta(days=1))
    items = [(u, created) for u in (soon, edge, far, past, disabled)]
    items.append((short, NOW - timedelta(hours=19)))  # total term 24h -> skipped
    assert [u.id for u in select_reminders(items, NOW)] == [1, 2]
    assert [u.id for u in select_reminders(items, NOW, already_reminded={1})] == [2]
    longer = (short, NOW - timedelta(days=2))
    assert [u.id for u in select_reminders([longer], NOW)] == [5]
