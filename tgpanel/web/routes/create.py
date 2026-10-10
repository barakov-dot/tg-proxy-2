"""Creating users: single, batch by count, batch by list. Links appear only after apply."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response

from tgpanel.apply.errors import OperationRejected
from tgpanel.domain.expiry import Term, default_expiry
from tgpanel.domain.models import CarrierMode, clean_display_name
from tgpanel.services.api import NewUser
from tgpanel.services.errors import UserServiceError
from tgpanel.web.inputs import parse_day, parse_uint
from tgpanel.web.routes.common import (
    HttpError,
    auth_of,
    clean,
    fraw,
    fstr,
    get_web,
    load_form,
    panel_tz,
    render,
    require_auth,
)
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])

MAX_BATCH = 200
TERMS = ("default", *(t.value for t in Term))


class FormError(Exception):
    pass


def parse_list(text: str) -> list[NewUser]:
    """Lines ``profile name; telegram id; comment; display name`` (last three optional)."""
    users: list[NewUser] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";", 3)]
        name = parts[0]
        raw_id = parts[1] if len(parts) > 1 else ""
        comment = parts[2] if len(parts) > 2 else ""
        display = parts[3] if len(parts) > 3 else ""
        if not name:
            raise FormError(T["create_line_noname"].format(n=lineno))
        tg_id: int | None = None
        if raw_id:
            tg_id = parse_uint(raw_id, max_digits=16)
            if tg_id is None or tg_id == 0:
                raise FormError(T["create_line_badid"].format(n=lineno))
        try:
            display = clean_display_name(display)
        except ValueError:
            raise FormError(T["create_line_badname"].format(n=lineno)) from None
        users.append(NewUser(name=name, tg_id=tg_id, comment=comment, display_name=display))
    return users


def parse_term(form_term: str, date_raw: str, tz: ZoneInfo, now: datetime) -> datetime | None:
    """``None`` = default term from the settings."""
    if form_term not in TERMS:
        raise FormError(T["bad_request"])
    if form_term == "default":
        return None
    term = Term(form_term)
    explicit: datetime | None = None
    if term is Term.DATE:
        day = parse_day(date_raw)
        if day is None:
            raise FormError(T["bad_date"])
        explicit = day.replace(hour=23, minute=59, second=59, tzinfo=tz).astimezone(UTC)
    try:
        return default_expiry(term, now, explicit)
    except ValueError:
        raise FormError(T["bad_date"]) from None


def _display(form: Any) -> str:
    try:
        return clean_display_name(fraw(form, "display_name"))
    except ValueError:
        raise FormError(T["bad_display_name"]) from None


def build_users(
    form_mode: str, form: Any, expires: datetime | None, mode: CarrierMode | None
) -> list[NewUser]:
    if form_mode == "single":
        name = fstr(form, "name")
        if not name:
            raise FormError(T["create_noname"])
        raw_id = fstr(form, "tg_id")
        single_id = parse_uint(raw_id, max_digits=16) if raw_id else None
        if raw_id and (single_id is None or single_id == 0):
            raise FormError(T["bad_number"])
        users = [
            NewUser(
                name=name,
                tg_id=single_id,
                comment=fraw(form, "comment").strip(),
                display_name=_display(form),
            )
        ]
    elif form_mode == "count":
        prefix = fstr(form, "prefix")
        raw_n = fstr(form, "count")
        if not prefix:
            raise FormError(T["create_noprefix"])
        n = parse_uint(raw_n, max_digits=4) or 0
        if not 1 <= n <= MAX_BATCH:
            raise FormError(T["create_badcount"].format(limit=MAX_BATCH))
        width = len(str(n))
        comment = fraw(form, "comment").strip()
        label = _display(form)
        users = [
            NewUser(
                name=f"{prefix}-{i:0{width}d}",
                comment=comment,
                display_name=f"{label} {i:0{width}d}" if label else "",
            )
            for i in range(1, n + 1)
        ]
    elif form_mode == "list":
        users = parse_list(fraw(form, "list"))
        if not users:
            raise FormError(T["create_emptylist"])
        if len(users) > MAX_BATCH:
            raise FormError(T["create_badcount"].format(limit=MAX_BATCH))
    else:
        raise FormError(T["bad_request"])
    return [
        NewUser(
            u.name,
            u.tg_id,
            u.comment,
            expires_at=expires,
            carrier_mode=mode,
            display_name=u.display_name,
        )
        for u in users
    ]


@router.get("/users/new")
async def create_form(request: Request) -> Response:
    return await render(request, "user_new.html", page="create", values={}, error=None, terms=TERMS)


@router.post("/users/new")
async def create_submit(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    tz = await panel_tz(web)
    values = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}

    async def fail(message: str, status: int) -> Response:
        return await render(
            request,
            "user_new.html",
            status,
            page="create",
            values=values,
            error=message,
            terms=TERMS,
        )

    try:
        expires = parse_term(fstr(form, "term"), fstr(form, "term_date"), tz, web.now())
        carrier_raw = fstr(form, "carrier")
        try:
            mode = CarrierMode(carrier_raw) if carrier_raw else None
        except ValueError:
            raise FormError(T["bad_request"]) from None
        new_users = build_users(fstr(form, "mode"), form, expires, mode)
    except FormError as exc:
        return await fail(str(exc), 422)
    except HttpError as exc:
        return await fail(exc.message, 422)

    try:
        result = await web.app.users.create(new_users, auth.actor)
    except (UserServiceError, OperationRejected) as exc:
        return await fail(clean(str(exc)), 409)
    if not result.ok:
        # nothing was created and no link exists: show the error, keep the form
        return await fail(clean(result.error) or T["operation_failed"], 409)
    created: list[Any] = []
    for uid in result.user_ids:
        user = await web.app.users.get(uid)
        if user is not None:
            created.append(user)
    return await render(request, "user_created.html", page="create", created=created)
