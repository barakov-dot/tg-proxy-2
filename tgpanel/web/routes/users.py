"""User list, bulk actions, user card, reveal, traffic JSON."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import segno
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from markupsafe import Markup

from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.domain.models import CarrierMode, UserRecord
from tgpanel.services.api import OperationResult, UserListQuery, UserRow
from tgpanel.services.errors import UserServiceError
from tgpanel.web.deps import WebContext, safe
from tgpanel.web.inputs import parse_day, parse_uint, valid_username
from tgpanel.web.listing import COLUMN_KEYS, parse_list_params
from tgpanel.web.routes.common import (
    HttpError,
    PathId,
    auth_of,
    clean,
    fids,
    fint,
    flist,
    fstr,
    get_web,
    load_form,
    panel_tz,
    redirect,
    render,
    require_auth,
    set_cookie,
)
from tgpanel.web.security import COLS_COOKIE
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])
log = logging.getLogger("tgpanel.web")

MAX_BULK = 10_000
MAX_REVEAL_MANY = 200
PRESET_DELTAS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def qr_svg(link: str) -> Markup:
    """QR code as an inline SVG generated on the server (no external service)."""
    svg = segno.make(link, error="m").svg_inline(scale=5, border=2, dark="#000000", light="#ffffff")
    return Markup(svg)  # noqa: S704 - produced by segno from a link, no user markup


# ------------------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class RowView:
    row: UserRow
    extra: repo.UserExtra | None

    @property
    def user(self) -> UserRecord:
        return self.row.user


async def _extras(web: WebContext, ids: list[int]) -> dict[int, repo.UserExtra | None]:
    def load(conn: sqlite3.Connection) -> dict[int, repo.UserExtra | None]:
        return {i: repo.get_user_extra(conn, i) for i in ids}

    return await web.app.db.run(load)


def _multi(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        out.setdefault(key, []).append(value)
    return out


def _visible_columns(request: Request, params: dict[str, list[str]]) -> tuple[list[str], bool]:
    """Columns from the URL (``cols_set`` + ``col``), else the cookie, else all. -> (cols, new)."""
    if "cols_set" in params:
        chosen = [c for c in params.get("col", []) if c in COLUMN_KEYS]
        return (chosen or list(COLUMN_KEYS)), True
    cookie = request.cookies.get(COLS_COOKIE, "")
    chosen = [c for c in cookie.split(",") if c in COLUMN_KEYS]
    return (chosen or list(COLUMN_KEYS)), False


async def collect_ids(web: WebContext, query: UserListQuery) -> list[int]:
    """Ids of ALL users matching the filter (every page), bounded by ``MAX_BULK``."""
    ids: list[int] = []
    page = 1
    while True:
        res = await web.app.users.list(
            replace(query, page=page, per_page=200, sort="id", descending=False)
        )
        ids.extend(r.user.id for r in res.rows)
        if len(ids) >= res.total or not res.rows:
            return ids
        if len(ids) > MAX_BULK:
            raise HttpError(400, T["too_many"].format(limit=MAX_BULK))
        page += 1


def _result_flash(result: OperationResult, ok_text: str) -> tuple[str, str]:
    if result.ok:
        return "ok", ok_text
    return "err", clean(result.error) or T["operation_failed"]


# ---------------------------------------------------------------------------- list


@router.get("/users")
async def users_list(request: Request) -> Response:
    web = get_web(request)
    tz = await panel_tz(web)
    params = _multi(request)
    query, canon = parse_list_params(params, tz)
    cols, persist = _visible_columns(request, params)
    page = await web.app.users.list(query)
    if page.total and not page.rows and query.page > 1:
        raise HttpError(404, T["not_found"])
    extras = await _extras(web, [r.user.id for r in page.rows])
    rows = [RowView(r, extras.get(r.user.id)) for r in page.rows]
    pages = max(1, -(-page.total // query.per_page))
    response = await render(
        request,
        "users.html",
        page="users",
        rows=rows,
        total=page.total,
        q=query,
        canon=canon,
        cols=cols,
        all_cols=COLUMN_KEYS,
        pages=pages,
        sort=query.sort,
        desc=query.descending,
        filter_qs=_encode(canon),
        statuses=canon.get("status", []),
    )
    if persist:
        set_cookie(response, request, COLS_COOKIE, ",".join(cols), 365 * 86400)
    return response


def _encode(canon: dict[str, list[str]]) -> str:
    from urllib.parse import urlencode

    return urlencode([(k, v) for k, vs in canon.items() for v in vs])


# ----------------------------------------------------------------------- bulk actions


@router.post("/users/bulk")
async def users_bulk(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    tz = await panel_tz(web)
    filter_qs = fstr(form, "filter_qs")
    back = "/users" + ("?" + filter_qs if filter_qs else "")
    action = fstr(form, "action")
    if fstr(form, "all_matching") == "1":
        try:
            parsed_qs = parse_qs(filter_qs, max_num_fields=60)
        except ValueError:
            raise HttpError(400, T["bad_filter"]) from None
        query, canon = parse_list_params(parsed_qs, tz)
        back = "/users" + ("?" + _encode(canon) if canon else "")
        ids = await collect_ids(web, query)
        expected = fint(form, "expected_total")
        if expected is None or expected != len(ids):
            return redirect(request, back, ("err", T["bulk_changed"]))
    else:
        ids = fids(form)
    if not ids:
        return redirect(request, back, ("err", T["bulk_none"]))
    if len(ids) > MAX_BULK:
        return redirect(request, back, ("err", T["too_many"].format(limit=MAX_BULK)))
    n = len(ids)
    actor = auth.actor
    flash: tuple[str, str]
    try:
        if action == "enable":
            flash = _result_flash(
                await web.app.users.set_status(ids, True, actor), T["done_n"].format(n=n)
            )
        elif action == "disable":
            flash = _result_flash(
                await web.app.users.set_status(ids, False, actor), T["done_n"].format(n=n)
            )
        elif action == "extend":
            days = fint(form, "days")
            if days is None:
                raise HttpError(400, T["bad_number"])
            flash = _result_flash(
                await web.app.users.extend(ids, days, actor), T["done_n"].format(n=n)
            )
        elif action == "set_comment":
            comment = fstr(form, "comment")
            changed = await web.bulk.set_comment(ids, comment, actor)
            flash = ("ok", T["done_n"].format(n=changed))
        elif action == "delete":
            if fstr(form, "confirm").lower() != T["delete_word"]:
                flash = ("err", T["delete_confirm_bad_bulk"].format(word=T["delete_word"]))
            else:
                flash = _result_flash(
                    await web.app.users.delete(ids, actor), T["deleted_n"].format(n=n)
                )
        elif action == "send_link":
            results = await safe(web.broadcast.send_links(ids, actor), "send_links")
            if results is None:
                flash = ("err", T["bot_unavailable"])
            else:
                sent = sum(1 for r in results if r.ok)
                failed = [r for r in results if not r.ok]
                text = T["links_sent"].format(sent=sent, failed=len(failed))
                if failed:
                    text += " " + "; ".join(
                        f"#{r.user_id}: {clean(r.note, 80)}" for r in failed[:5]
                    )
                flash = ("ok" if sent else "err", text)
        else:
            raise HttpError(400, T["bad_request"])
    except (UserServiceError, OperationRejected) as exc:
        flash = ("err", clean(str(exc)))
    return redirect(request, back, flash)


# ------------------------------------------------------------------- inline comment


@router.post("/users/{user_id}/comment")
async def user_comment(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    comment = str(form.get("inline_comment") or "")
    error: str | None = None
    try:
        await web.app.users.update_meta(user_id, auth.actor, comment=comment)
    except (UserServiceError, OperationRejected) as exc:
        error = clean(str(exc))
    user = await web.app.users.get(user_id)
    if user is None:
        raise HttpError(404, T["not_found"])
    return await render(
        request,
        "_comment_cell.html",
        422 if error else 200,
        u=user,
        error=error,
        saved=error is None,
    )


# --------------------------------------------------------------------------- card


async def _card_context(web: WebContext, user: UserRecord) -> dict[str, Any]:
    extra = await web.app.db.run(repo.get_user_extra, user.id)
    events = await web.app.db.run(lambda c: repo.list_audit(c, target=f"user:{user.id}", limit=50))
    cfg = await web.app.settings.snapshot()
    return {
        "u": user,
        "extra": extra,
        "events": events,
        "modes": [m.value for m in CarrierMode],
        "default_mode": cfg.carrier_mode_default.value,
    }


@router.get("/users/{user_id}")
async def user_card(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    user = await web.app.users.get(user_id)
    if user is None:
        raise HttpError(404, T["not_found"])
    return await render(request, "user_card.html", page="users", **await _card_context(web, user))


@router.post("/users/{user_id}/meta")
async def user_meta(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    user = await web.app.users.get(user_id)
    if user is None:
        raise HttpError(404, T["not_found"])
    name = fstr(form, "name")
    comment = str(form.get("comment") or "")
    username = fstr(form, "tg_username").lstrip("@")
    tg_raw = fstr(form, "tg_id")
    clear_tg = fstr(form, "clear_tg_id") == "1"
    if not valid_username(username):
        return redirect(request, f"/users/{user_id}", ("err", T["bad_username"]))
    fields: dict[str, Any] = {}
    if name and name != user.name:
        fields["name"] = name
    if comment != user.comment:
        fields["comment"] = comment
    tg_new: int | None = None
    if tg_raw and not clear_tg:
        tg_new = parse_uint(tg_raw, max_digits=16)
        if tg_new is None or tg_new == 0:
            return redirect(request, f"/users/{user_id}", ("err", T["bad_number"]))
        if tg_new != user.tg_id:
            fields["tg_id"] = tg_new
    extra = await web.app.db.run(repo.get_user_extra, user_id)
    if extra is not None and username != (extra.tg_username or ""):
        fields["tg_username"] = username
    cleared = False
    if clear_tg and user.tg_id is not None:
        cleared = True
    if not fields and not cleared:
        return redirect(request, f"/users/{user_id}", ("ok", T["nothing_changed"]))
    try:
        if fields:
            await web.app.users.update_meta(user_id, auth.actor, **fields)
        if cleared:
            await web.bulk.clear_tg_id(user_id, auth.actor)
    except (UserServiceError, OperationRejected) as exc:
        return redirect(request, f"/users/{user_id}", ("err", clean(str(exc))))
    return redirect(request, f"/users/{user_id}", ("ok", T["saved"]))


def _end_of_day(raw: str, tz: ZoneInfo) -> datetime:
    day = parse_day(raw)
    if day is None:
        raise HttpError(400, T["bad_date"])
    return day.replace(hour=23, minute=59, second=59, tzinfo=tz).astimezone(ZoneInfo("UTC"))


@router.post("/users/{user_id}/action")
async def user_action(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    tz = await panel_tz(web)
    user = await web.app.users.get(user_id)
    if user is None:
        raise HttpError(404, T["not_found"])
    action = fstr(form, "action")
    actor = auth.actor
    back = f"/users/{user_id}"
    users = web.app.users
    flash: tuple[str, str]
    try:
        if action == "enable":
            flash = _result_flash(await users.set_status([user_id], True, actor), T["saved"])
        elif action == "disable":
            flash = _result_flash(await users.set_status([user_id], False, actor), T["saved"])
        elif action == "extend":
            days = fint(form, "days")
            if days is None:
                raise HttpError(400, T["bad_number"])
            flash = _result_flash(await users.extend([user_id], days, actor), T["saved"])
        elif action == "set_expiry":
            when = _end_of_day(fstr(form, "expires_date"), tz)
            if when <= web.now() and fstr(form, "confirm_past") != "1":
                return redirect(request, back, ("err", T["expiry_past_confirm"]))
            flash = _result_flash(await users.set_expiry([user_id], when, actor), T["saved"])
        elif action == "clear_expiry":
            flash = _result_flash(await users.set_expiry([user_id], None, actor), T["saved"])
        elif action == "carrier":
            raw = fstr(form, "carrier")
            mode = CarrierMode(raw) if raw else None
            flash = _result_flash(await users.set_carrier_mode([user_id], mode, actor), T["saved"])
        elif action == "reissue":
            if fstr(form, "confirm") != "1":
                flash = ("err", T["reissue_confirm_needed"])
            else:
                flash = _result_flash(await users.reissue_secret(user_id, actor), T["reissued"])
        elif action == "send_link":
            results = await safe(web.broadcast.send_links([user_id], actor), "send_links")
            if not results:
                flash = ("err", T["bot_unavailable"])
            elif results[0].ok:
                flash = ("ok", T["link_sent"])
            else:
                flash = ("err", clean(results[0].note) or T["operation_failed"])
        elif action == "delete":
            if fstr(form, "confirm_name") != user.name:
                return redirect(request, back, ("err", T["delete_name_mismatch"]))
            result = await users.delete([user_id], actor)
            if result.ok:
                return redirect(request, "/users", ("ok", T["deleted"]))
            flash = _result_flash(result, "")
        else:
            raise HttpError(400, T["bad_request"])
    except ValueError:
        raise HttpError(400, T["bad_request"]) from None
    except (UserServiceError, OperationRejected) as exc:
        flash = ("err", clean(str(exc)))
    return redirect(request, back, flash)


# -------------------------------------------------------------------------- reveal


async def reveal_block(request: Request, web: WebContext, user: UserRecord, actor: str) -> Response:
    try:
        await web.app.users.load_hostname()
        https = web.app.users.link(user)
        tg = web.app.users.tg_link(user)
    except UserServiceError as exc:
        return await render(request, "_reveal.html", 409, u=user, error=clean(str(exc)))

    try:
        await web.admin.audit(actor, "user.reveal", f"user:{user.id}")
    except OperationRejected:
        log.warning("reveal audit skipped: database busy")
    return await render(
        request,
        "_reveal.html",
        u=user,
        error=None,
        https_link=https,
        tg_link=tg,
        secret=user.secret,
        qr=qr_svg(https),
    )


@router.post("/users/{user_id}/reveal")
async def user_reveal(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    user = await web.app.users.get(user_id)
    if user is None:
        raise HttpError(404, T["not_found"])
    return await reveal_block(request, web, user, auth.actor)


@router.post("/users/reveal-many")
async def users_reveal_many(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    ids = fids(form)[:MAX_REVEAL_MANY]
    if not ids:
        raise HttpError(400, T["bulk_none"])
    try:
        await web.app.users.load_hostname()
        lines: list[str] = []
        for uid in ids:
            user = await web.app.users.get(uid)
            if user is not None:
                lines.append(f"{user.name}: {web.app.users.link(user)}")
    except UserServiceError as exc:
        return await render(request, "_reveal_many.html", 409, error=clean(str(exc)), text="")

    try:
        await web.admin.audit(auth.actor, "user.reveal", "bulk", f"count={len(ids)}")
    except OperationRejected:
        log.warning("reveal audit skipped: database busy")
    return await render(request, "_reveal_many.html", error=None, text="\n".join(lines))


# ------------------------------------------------------------------------- traffic


@router.get("/users/{user_id}/traffic.json")
async def user_traffic(request: Request, user_id: PathId) -> Response:
    web = get_web(request)
    preset = request.query_params.get("preset", "30d")
    if preset not in (*PRESET_DELTAS, "all"):
        raise HttpError(400, T["bad_filter"])
    if await web.app.users.get(user_id) is None:
        raise HttpError(404, T["not_found"])
    end = web.now()
    start: datetime | None
    if preset == "all":
        extra = await web.app.db.run(repo.get_user_extra, user_id)
        start = extra.created_at if extra is not None else None
    else:
        start = end - PRESET_DELTAS[preset]
    series = await safe(web.traffic.user_series(user_id, start, end, 600), "user_series")
    if series is None:
        return JSONResponse({"error": T["traffic_unavailable"]}, status_code=503)
    points: list[list[float | int]] = []
    for p in series.points:
        ts, up, down = (p.ts, p.up, p.down) if hasattr(p, "ts") else (p[0], p[1], p[2])
        ms = int(ts.timestamp() * 1000) if isinstance(ts, datetime) else int(float(ts) * 1000)
        points.append([ms, int(up), int(down)])
    return JSONResponse(
        {
            "granularity": series.granularity,
            "points": points,
            "total_up": int(series.total_up),
            "total_down": int(series.total_down),
        }
    )


__all__ = ["flist", "router"]
