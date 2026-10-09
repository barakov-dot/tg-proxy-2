"""Parsing and whitelisting of the user-list query string.

Every value is validated and re-serialised from the parsed result; request strings are never
passed on to SQL (the repository maps ``SortField`` names to fixed SQL expressions).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import get_args
from zoneinfo import ZoneInfo

from tgpanel.domain.models import UserStatus
from tgpanel.domain.queries import PER_PAGE_CHOICES, Period, SortField, UserFilter, UserListQuery
from tgpanel.web.routes.common import HttpError
from tgpanel.web.texts import T

SORT_FIELDS: tuple[str, ...] = get_args(SortField)
PERIODS: tuple[str, ...] = get_args(Period)
COLUMN_KEYS: tuple[str, ...] = SORT_FIELDS  # every column is sortable
MAX_PAGE = 100_000
MAX_TEXT = 200
MB = 1024 * 1024


def _one(params: Mapping[str, Sequence[str]], key: str) -> str:
    values = params.get(key) or [""]
    return values[0].strip()


def _tri(params: Mapping[str, Sequence[str]], key: str) -> bool | None:
    raw = _one(params, key)
    if raw == "":
        return None
    if raw not in ("0", "1"):
        raise HttpError(400, T["bad_filter"])
    return raw == "1"


def _int(params: Mapping[str, Sequence[str]], key: str, low: int, high: int) -> int | None:
    raw = _one(params, key)
    if not raw:
        return None
    if not raw.isdigit() or len(raw) > 12 or not low <= int(raw) <= high:
        raise HttpError(400, T["bad_filter"])
    return int(raw)


def _mb(params: Mapping[str, Sequence[str]], key: str) -> int | None:
    raw = _one(params, key).replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise HttpError(400, T["bad_filter"]) from None
    if not 0 <= value < 1e9:
        raise HttpError(400, T["bad_filter"])
    return int(value * MB)


def _date(
    params: Mapping[str, Sequence[str]], key: str, tz: ZoneInfo, end: bool
) -> datetime | None:
    raw = _one(params, key)
    if not raw:
        return None
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise HttpError(400, T["bad_filter"]) from None
    if end:
        day = day + timedelta(days=1) - timedelta(seconds=1)
    return day.astimezone(ZoneInfo("UTC"))


def _text(params: Mapping[str, Sequence[str]], key: str) -> str | None:
    raw = _one(params, key)
    if len(raw) > MAX_TEXT:
        raise HttpError(400, T["bad_filter"])
    return raw or None


def parse_list_params(
    params: Mapping[str, Sequence[str]], tz: ZoneInfo
) -> tuple[UserListQuery, dict[str, list[str]]]:
    """Return the validated query and its canonical (whitelisted) query-string parameters."""
    sort = _one(params, "sort") or "id"
    if sort not in SORT_FIELDS:
        raise HttpError(400, T["bad_sort"])
    direction = _one(params, "dir") or "asc"
    if direction not in ("asc", "desc"):
        raise HttpError(400, T["bad_sort"])
    period = _one(params, "period") or "30d"
    if period not in PERIODS:
        raise HttpError(400, T["bad_filter"])
    per_raw = _one(params, "per") or str(PER_PAGE_CHOICES[0])
    if not per_raw.isdigit() or int(per_raw) not in PER_PAGE_CHOICES:
        raise HttpError(400, T["bad_filter"])
    page = _int(params, "page", 1, MAX_PAGE) or 1

    statuses: list[UserStatus] = []
    for raw in params.get("status") or []:
        try:
            status = UserStatus(raw)
        except ValueError:
            raise HttpError(400, T["bad_filter"]) from None
        if status not in statuses:
            statuses.append(status)

    flt = UserFilter(
        query=_text(params, "q"),
        statuses=tuple(statuses),
        online=_tri(params, "online"),
        imported=_tri(params, "imported"),
        has_tg_id=_tri(params, "tg"),
        bot_started=_tri(params, "bot"),
        expires_within_days=_int(params, "expires_days", 1, 3650),
        created_from=_date(params, "created_from", tz, end=False),
        created_to=_date(params, "created_to", tz, end=True),
        last_seen_from=_date(params, "seen_from", tz, end=False),
        last_seen_to=_date(params, "seen_to", tz, end=True),
        traffic_min=_mb(params, "traffic_min"),
        traffic_max=_mb(params, "traffic_max"),
        comment_contains=_text(params, "comment"),
    )
    per_page = int(per_raw)
    query = UserListQuery(
        filter=flt,
        sort=sort,  # type: ignore[arg-type]  # validated against the Literal above
        descending=direction == "desc",
        period=period,  # type: ignore[arg-type]
        page=page,
        per_page=per_page,  # type: ignore[arg-type]
    )

    canon: dict[str, list[str]] = {}
    for key in (
        "q",
        "online",
        "imported",
        "tg",
        "bot",
        "expires_days",
        "created_from",
        "created_to",
        "seen_from",
        "seen_to",
        "traffic_min",
        "traffic_max",
        "comment",
    ):
        value = _one(params, key)
        if value:
            canon[key] = [value]
    if statuses:
        canon["status"] = [s.value for s in statuses]
    if sort != "id":
        canon["sort"] = [sort]
    if direction != "asc":
        canon["dir"] = [direction]
    if period != "30d":
        canon["period"] = [period]
    if per_page != PER_PAGE_CHOICES[0]:
        canon["per"] = [str(per_page)]
    return query, canon
