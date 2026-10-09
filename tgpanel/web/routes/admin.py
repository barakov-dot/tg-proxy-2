"""Access requests, broadcast, settings, backups, audit/apply history."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from tgpanel.apply.backup import BackupError
from tgpanel.apply.errors import OperationRejected, SettingsError
from tgpanel.apply.settings_spec import SPECS
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.domain.models import CarrierMode, UserStatus
from tgpanel.services.admin import MAX_TEMPLATE, MESSAGE_KEYS
from tgpanel.services.api import UserFilter, UserListQuery
from tgpanel.web.deps import WebContext, safe
from tgpanel.web.inputs import parse_uint
from tgpanel.web.routes.common import (
    HttpError,
    PathId,
    auth_of,
    clean,
    delete_cookie,
    fint,
    fraw,
    fstr,
    get_signer,
    get_web,
    load_form,
    redirect,
    render,
    require_auth,
    set_cookie,
)
from tgpanel.web.routes.users import collect_ids
from tgpanel.web.security import SESSION_COOKIE, SESSION_TTL_S
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])

BCAST_TOKEN_TTL_S = 3600


# ------------------------------------------------------------------------ requests


@router.get("/requests")
async def requests_page(request: Request) -> Response:
    web = get_web(request)
    pending = await web.app.db.run(repo.list_access_requests, "pending")
    decided = [r for r in await web.app.db.run(repo.list_access_requests) if r.status != "pending"][
        :50
    ]
    return await render(
        request,
        "requests.html",
        page="requests",
        pending=pending,
        decided=decided,
        terms=[t.value for t in Term if t is not Term.DATE],
    )


@router.post("/requests/{request_id}/approve")
async def request_approve(request: Request, request_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    raw = fstr(form, "term")
    term: Term | None
    if raw in ("", "default"):
        term = None
    else:
        try:
            term = Term(raw)
        except ValueError:
            raise HttpError(400, T["bad_request"]) from None
        if term is Term.DATE:
            raise HttpError(400, T["bad_request"])
    res = await safe(web.requests.approve(request_id, term, auth.actor), "approve")
    if res is None:
        return redirect(request, "/requests", ("err", T["bot_unavailable"]))
    if not res.ok:
        return redirect(request, "/requests", ("err", clean(res.error) or T["operation_failed"]))
    return redirect(request, "/requests", ("ok", T["request_approved"]))


@router.post("/requests/{request_id}/reject")
async def request_reject(request: Request, request_id: PathId) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    res = await safe(web.requests.reject(request_id, auth.actor), "reject")
    if res is None:
        return redirect(request, "/requests", ("err", T["bot_unavailable"]))
    if not res.ok:
        return redirect(request, "/requests", ("err", clean(res.error) or T["operation_failed"]))
    return redirect(request, "/requests", ("ok", T["request_rejected"]))


# ----------------------------------------------------------------------- broadcast


AUDIENCES = {
    "active": UserFilter(statuses=(UserStatus.ACTIVE,), bot_started=True),
    "all": UserFilter(bot_started=True),
}


async def _template_default(web: WebContext) -> str:
    return str(await web.app.db.run(repo.get_setting, "msg.broadcast", "") or "")


@router.get("/broadcast")
async def broadcast_form(request: Request) -> Response:
    web = get_web(request)
    return await render(
        request,
        "broadcast.html",
        page="broadcast",
        template=await _template_default(web),
        audience="active",
        preview=None,
        form_token="",
        error=None,
    )


@router.post("/broadcast/preview")
async def broadcast_preview(request: Request) -> Response:
    web = get_web(request)
    form = await load_form(request)
    audience = fstr(form, "audience")
    template = fraw(form, "template").strip()
    if audience not in AUDIENCES or not template or len(template) > MAX_TEMPLATE:
        return await render(
            request,
            "broadcast.html",
            422,
            page="broadcast",
            template=template,
            audience="active",
            preview=None,
            form_token="",
            error=T["broadcast_bad_form"],
        )
    ids = await collect_ids(web, UserListQuery(filter=AUDIENCES[audience]))
    preview = await safe(web.broadcast.preview(template, ids), "broadcast.preview")
    error = None if preview is not None else T["bot_unavailable"]
    token = get_signer(request).dumps(
        "bcast",
        {"n": secrets.token_urlsafe(12), "e": int(web.now().timestamp()) + BCAST_TOKEN_TTL_S},
    )
    return await render(
        request,
        "broadcast.html",
        page="broadcast",
        template=template,
        audience=audience,
        preview=preview,
        form_token=token,
        error=error or (clean(preview.error) if preview and preview.error else None),
    )


@router.post("/broadcast/start")
async def broadcast_start(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    audience = fstr(form, "audience")
    template = fraw(form, "template").strip()
    if audience not in AUDIENCES or not template or len(template) > MAX_TEMPLATE:
        return redirect(request, "/broadcast", ("err", T["broadcast_bad_form"]))
    payload = get_signer(request).loads("bcast", fstr(form, "form_token"))
    nonce = str(payload.get("n", "")) if payload else ""
    if (
        not nonce
        or int(payload.get("e", 0)) < int(web.now().timestamp())  # type: ignore[union-attr]
        or nonce in web.used_tokens
    ):
        return redirect(request, "/broadcast", ("err", T["broadcast_token_used"]))
    ids = await collect_ids(web, UserListQuery(filter=AUDIENCES[audience]))
    if not ids:
        return redirect(request, "/broadcast", ("err", T["broadcast_nobody"]))
    if len(web.used_tokens) > 2000:
        web.used_tokens.clear()
    web.used_tokens.add(nonce)  # spent: a double click or a reload cannot send twice
    bid = await safe(web.broadcast.start(template, ids, auth.actor), "broadcast.start")
    if bid is None:
        return redirect(request, "/broadcast", ("err", T["bot_unavailable"]))
    return redirect(request, f"/broadcast/{bid}")


@router.get("/broadcast/{broadcast_id}")
async def broadcast_report(request: Request, broadcast_id: PathId) -> Response:
    web = get_web(request)
    report = await safe(web.broadcast.report(broadcast_id), "broadcast.report")
    if report is None:
        raise HttpError(404, T["not_found"])
    return await render(request, "broadcast_report.html", page="broadcast", r=report)


@router.get("/broadcast/{broadcast_id}/report")
async def broadcast_report_fragment(request: Request, broadcast_id: PathId) -> Response:
    web = get_web(request)
    report = await safe(web.broadcast.report(broadcast_id), "broadcast.report")
    if report is None:
        raise HttpError(404, T["not_found"])
    return await render(request, "_broadcast_report.html", r=report)


# ------------------------------------------------------------------------- settings

CHOICES: dict[str, list[tuple[str, str]]] = {
    "default_term": [(t.value, T[f"term_{t.value}"]) for t in Term],
    "carrier_mode_default": [(m.value, m.value) for m in CarrierMode],
    "issuance_mode": [("approval", T["issuance_approval"]), ("open", T["issuance_open"])],
}
FIELD_TYPES = {
    "default_term_date": "date",
    "secrets_per_process": "number",
    "max_sessions_global": "number",
    "max_streams_global": "number",
    "mtp_max_connections": "number",
    "mtp_workers": "number",
    "backup_keep_last": "number",
    "backup_keep_days": "number",
    "poll_interval_s": "number",
    "retention_minute_days": "number",
    "retention_hour_days": "number",
    "activity_min_bytes": "number",
    "activity_min_packets": "number",
    "open_mode_max_per_hour": "number",
    "reminder_days": "number",
    "backup_hour": "number",
}


async def _settings_context(
    web: WebContext, values: dict[str, str] | None = None, errors: dict[str, str] | None = None
) -> dict[str, Any]:
    current = await web.app.settings.all()
    fields = []
    for key, spec in SPECS.items():
        raw = values[key] if values is not None and key in values else str(current[key])
        fields.append(
            {
                "key": key,
                "title": spec.title,
                "value": raw,
                "choices": CHOICES.get(key),
                "type": FIELD_TYPES.get(key, "text"),
                "proxy": spec.affects_proxy,
                "error": (errors or {}).get(key),
            }
        )
    templates = await web.admin.templates()
    return {
        "fields": fields,
        "admins": await web.admin.list_admins(),
        "token_set_at": await web.admin.bot_token_set_at(),
        "templates": [
            {"key": k, "label": T[f"tpl_{k.split('.', 1)[1]}"], "value": templates[k]}
            for k in MESSAGE_KEYS
        ],
    }


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    web = get_web(request)
    return await render(request, "settings.html", page="settings", **await _settings_context(web))


@router.post("/settings")
async def settings_save(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    current = await web.app.settings.all()
    submitted = {k: fstr(form, k) for k in SPECS if k in form}
    errors: dict[str, str] = {}
    changed: dict[str, str] = {}
    for key, raw in submitted.items():
        if FIELD_TYPES.get(key) == "number" and parse_uint(raw, max_digits=9) is None:
            errors[key] = T["bad_number"]
            continue
        try:
            new = web.app.settings.validate({key: raw})[key]
            old = web.app.settings.validate({key: str(current[key])})[key]
        except SettingsError as exc:
            errors[key] = str(exc)
            continue
        if new != old:
            changed[key] = new
    if errors:
        return await render(
            request,
            "settings.html",
            422,
            page="settings",
            **await _settings_context(web, submitted, errors),
        )
    if not changed:
        return redirect(request, "/settings", ("ok", T["nothing_changed"]))
    result = await web.app.settings.set_many(changed, auth.actor)
    if not result.ok:
        return redirect(request, "/settings", ("err", clean(result.error) or T["operation_failed"]))
    return redirect(request, "/settings", ("ok", T["saved"]))


@router.post("/settings/admins")
async def settings_admins(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    action = fstr(form, "action")
    tg_id = fint(form, "tg_id")
    if tg_id is None or not 0 < tg_id <= 2**53 or action not in ("add", "remove"):
        return redirect(request, "/settings", ("err", T["bad_number"]))
    try:
        if action == "add":
            await web.admin.add_admin(tg_id, auth.actor)
        else:
            await web.admin.remove_admin(tg_id, auth.actor)
    except OperationRejected as exc:
        return redirect(request, "/settings", ("err", clean(str(exc))))
    return redirect(request, "/settings", ("ok", T["saved"]))


@router.post("/settings/bot-token")
async def settings_bot_token(request: Request) -> Response:
    web = get_web(request)
    form = await load_form(request)
    try:
        await web.admin.set_bot_token(fstr(form, "token"), auth_of(request).actor)
    except OperationRejected as exc:
        return redirect(request, "/settings", ("err", clean(str(exc))))
    return redirect(request, "/settings", ("ok", T["token_saved"]))


@router.post("/settings/templates")
async def settings_templates(request: Request) -> Response:
    web = get_web(request)
    form = await load_form(request)
    values = {key: fraw(form, key) for key in MESSAGE_KEYS}
    try:
        await web.admin.set_templates(values, auth_of(request).actor)
    except OperationRejected as exc:
        return redirect(request, "/settings", ("err", clean(str(exc))))
    return redirect(request, "/settings", ("ok", T["saved"]))


@router.post("/settings/password")
async def settings_password(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    try:
        version = await web.admin.change_password(
            fraw(form, "current"), fraw(form, "new"), fraw(form, "again"), auth.actor
        )
    except OperationRejected as exc:
        return redirect(request, "/settings", ("err", clean(str(exc))))
    signer = get_signer(request)
    session = signer.make_session(auth.session.login, version, int(web.now().timestamp()))
    response = redirect(request, "/settings", ("ok", T["password_changed"]))
    set_cookie(response, request, SESSION_COOKIE, signer.dump_session(session), SESSION_TTL_S)
    return response


@router.post("/settings/logout-all")
async def settings_logout_all(request: Request) -> Response:
    web = get_web(request)
    try:
        await web.admin.logout_all(auth_of(request).actor)
    except OperationRejected as exc:
        raise HttpError(503, clean(str(exc))) from None
    response = redirect(request, "/login")
    delete_cookie(response, request, SESSION_COOKIE)
    return response


# ------------------------------------------------------------------------- backups


@router.get("/backups")
async def backups_page(request: Request) -> Response:
    web = get_web(request)
    return await render(request, "backups.html", page="backups", items=await web.backups.list())


@router.post("/backups/create")
async def backups_create(request: Request) -> Response:
    web = get_web(request)
    try:
        await web.backups.create(auth_of(request).actor)
    except (BackupError, OperationRejected) as exc:
        return redirect(request, "/backups", ("err", clean(str(exc))))
    return redirect(request, "/backups", ("ok", T["backup_created"]))


@router.get("/backups/{backup_id}/download")
async def backup_download(request: Request, backup_id: PathId) -> Response:
    web = get_web(request)
    try:
        download = await web.backups.open_download(backup_id, auth_of(request).actor)
    except BackupError as exc:
        raise HttpError(413, clean(str(exc))) from None
    except OperationRejected as exc:
        raise HttpError(503, clean(str(exc))) from None
    if download is None:
        raise HttpError(404, T["not_found"])
    return StreamingResponse(
        download.chunks,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{download.filename}"',
            "Content-Length": str(download.size),
        },
    )


@router.post("/backups/{backup_id}/restore")
async def backup_restore(request: Request, backup_id: PathId) -> Response:
    web = get_web(request)
    form = await load_form(request)
    if fstr(form, "confirm").lower() != T["restore_word"]:
        return redirect(
            request, "/backups", ("err", T["restore_confirm_bad"].format(word=T["restore_word"]))
        )
    try:
        outcome = await web.backups.restore(backup_id, auth_of(request).actor)
    except (BackupError, OperationRejected) as exc:
        return redirect(request, "/backups", ("err", clean(str(exc))))
    if not outcome.ok:
        return redirect(request, "/backups", ("err", clean(outcome.error) or T["operation_failed"]))
    return redirect(request, "/backups", ("ok", T["restored"]))


# --------------------------------------------------------------------------- audit

PAGE_SIZE = 100


@router.get("/audit")
async def audit_page(request: Request) -> Response:
    web = get_web(request)
    tab = request.query_params.get("tab", "audit")
    page = parse_uint(request.query_params.get("page", "1"), max_digits=6) or 0
    if tab not in ("audit", "apply") or not 1 <= page <= 100000:
        raise HttpError(400, T["bad_filter"])
    offset = (page - 1) * PAGE_SIZE
    entries: list[Any]
    if tab == "audit":
        entries = await web.app.db.run(
            lambda c: repo.list_audit(c, limit=PAGE_SIZE + 1, offset=offset)
        )
    else:
        entries = await web.app.db.run(repo.list_apply_runs, PAGE_SIZE + 1, offset)
    more = len(entries) > PAGE_SIZE
    return await render(
        request,
        "audit.html",
        page="audit",
        tab=tab,
        entries=entries[:PAGE_SIZE],
        pg=page,
        more=more,
    )
