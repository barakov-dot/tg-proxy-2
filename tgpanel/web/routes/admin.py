"""Access requests, broadcast, settings, backups, audit/apply history."""

from __future__ import annotations

import posixpath
import re
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from tgpanel.apply.backup import BackupError
from tgpanel.apply.errors import OperationRejected, SettingsError
from tgpanel.apply.settings_spec import SPECS
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.domain.models import CarrierMode, UserStatus
from tgpanel.services.api import UserFilter, UserListQuery
from tgpanel.system.ops import SystemOpsError
from tgpanel.web.deps import WebContext, safe
from tgpanel.web.routes.common import (
    KEY_HASH,
    KEY_VERSION,
    HttpError,
    auth_of,
    clean,
    fint,
    fraw,
    fstr,
    get_signer,
    get_web,
    load_form,
    read_panel_auth,
    redirect,
    render,
    require_auth,
    set_cookie,
)
from tgpanel.web.routes.users import collect_ids
from tgpanel.web.security import SESSION_COOKIE, SESSION_TTL_S
from tgpanel.web.texts import T

router = APIRouter(dependencies=[Depends(require_auth)])

KEY_BOT_TOKEN = "bot_token"  # noqa: S105 - setting name
MESSAGE_KEYS = (
    "msg.link",
    "msg.welcome",
    "msg.approved",
    "msg.rejected",
    "msg.expiring",
    "msg.expired",
    "msg.broadcast",
)
MAX_TEMPLATE = 3500
MIN_PASSWORD = 12
_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,60}$")
_SECRET_LIKE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")
CHUNK = 1 << 20


async def db_write(web: WebContext, fn: Any) -> str | None:
    """Run a DB write via the pipeline lock; returns an error text or None."""
    try:
        await web.pipeline.db_write(fn)
    except OperationRejected as exc:
        return clean(str(exc))
    return None


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
async def request_approve(request: Request, request_id: int) -> Response:
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
async def request_reject(request: Request, request_id: int) -> Response:
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
            error=T["broadcast_bad_form"],
        )
    ids = await collect_ids(web, UserListQuery(filter=AUDIENCES[audience]))
    preview = await safe(web.broadcast.preview(template, ids), "broadcast.preview")
    error = None if preview is not None else T["bot_unavailable"]
    return await render(
        request,
        "broadcast.html",
        page="broadcast",
        template=template,
        audience=audience,
        preview=preview,
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
    ids = await collect_ids(web, UserListQuery(filter=AUDIENCES[audience]))
    if not ids:
        return redirect(request, "/broadcast", ("err", T["broadcast_nobody"]))
    bid = await safe(web.broadcast.start(template, ids, auth.actor), "broadcast.start")
    if bid is None:
        return redirect(request, "/broadcast", ("err", T["bot_unavailable"]))
    return redirect(request, f"/broadcast/{bid}")


@router.get("/broadcast/{broadcast_id}")
async def broadcast_report(request: Request, broadcast_id: int) -> Response:
    web = get_web(request)
    report = await safe(web.broadcast.report(broadcast_id), "broadcast.report")
    if report is None:
        raise HttpError(404, T["not_found"])
    return await render(request, "broadcast_report.html", page="broadcast", r=report)


@router.get("/broadcast/{broadcast_id}/report")
async def broadcast_report_fragment(request: Request, broadcast_id: int) -> Response:
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
    all_stored = await web.app.db.run(repo.all_settings)
    admins = await web.app.db.run(repo.list_admins)
    return {
        "fields": fields,
        "admins": admins,
        "token_set": bool(all_stored.get(KEY_BOT_TOKEN)),
        "templates": [
            {"key": k, "label": T[f"tpl_{k.split('.', 1)[1]}"], "value": all_stored.get(k, "")}
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

    def write(conn: sqlite3.Connection) -> None:
        if action == "add":
            repo.add_admin(conn, tg_id, web.now())
        else:
            repo.remove_admin(conn, tg_id)
        repo.add_audit(conn, web.now(), auth.actor, f"admin.{action}", f"tg:{tg_id}", "")

    error = await db_write(web, write)
    return redirect(request, "/settings", ("err", error) if error else ("ok", T["saved"]))


@router.post("/settings/bot-token")
async def settings_bot_token(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    token = fstr(form, "token")
    if not _TOKEN_RE.match(token):
        return redirect(request, "/settings", ("err", T["token_bad"]))

    def write(conn: sqlite3.Connection) -> None:
        repo.set_setting(conn, KEY_BOT_TOKEN, token)
        repo.add_audit(conn, web.now(), auth.actor, "settings.set", KEY_BOT_TOKEN, "***")

    error = await db_write(web, write)
    return redirect(request, "/settings", ("err", error) if error else ("ok", T["token_saved"]))


@router.post("/settings/templates")
async def settings_templates(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    values: dict[str, str] = {}
    for key in MESSAGE_KEYS:
        text = fraw(form, key).strip()
        if len(text) > MAX_TEMPLATE or _SECRET_LIKE.search(text):
            return redirect(request, "/settings", ("err", T["template_bad"]))
        values[key] = text

    def write(conn: sqlite3.Connection) -> None:
        for key, text in values.items():
            if text:
                repo.set_setting(conn, key, text)
            else:
                repo.delete_setting(conn, key)
        repo.add_audit(conn, web.now(), auth.actor, "settings.templates", "", "")

    error = await db_write(web, write)
    return redirect(request, "/settings", ("err", error) if error else ("ok", T["saved"]))


async def _bump_version(web: WebContext, actor: str, new_hash: str | None, action: str) -> int:
    holder: list[int] = []

    def write(conn: sqlite3.Connection) -> None:
        cfg = read_panel_auth(conn)
        version = cfg.version + 1
        holder.append(version)
        repo.set_setting(conn, KEY_VERSION, str(version))
        if new_hash is not None:
            repo.set_setting(conn, KEY_HASH, new_hash)
        repo.add_audit(conn, web.now(), actor, action, "", "")

    error = await db_write(web, write)
    if error:
        raise HttpError(503, error)
    return holder[0]


@router.post("/settings/password")
async def settings_password(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    current = fraw(form, "current")
    new = fraw(form, "new")
    again = fraw(form, "again")
    cfg = await web.app.db.run(read_panel_auth)
    try:
        web.password_hasher.verify(cfg.password_hash, current)
    except Exception:  # wrong password or bad stored hash
        return redirect(request, "/settings", ("err", T["password_wrong"]))
    if len(new) < MIN_PASSWORD or len(new) > 1024:
        return redirect(request, "/settings", ("err", T["password_short"].format(n=MIN_PASSWORD)))
    if new != again:
        return redirect(request, "/settings", ("err", T["password_mismatch"]))
    version = await _bump_version(
        web, auth.actor, web.password_hasher.hash(new), "web.password_change"
    )
    signer = get_signer(request)
    session = signer.make_session(cfg.login, version, int(web.now().timestamp()))
    response = redirect(request, "/settings", ("ok", T["password_changed"]))
    set_cookie(response, request, SESSION_COOKIE, signer.dump_session(session), SESSION_TTL_S)
    return response


@router.post("/settings/logout-all")
async def settings_logout_all(request: Request) -> Response:
    web = get_web(request)
    await _bump_version(web, auth_of(request).actor, None, "web.logout_all")
    from tgpanel.web.routes.common import delete_cookie

    response = redirect(request, "/login")
    delete_cookie(response, request, SESSION_COOKIE)
    return response


# ------------------------------------------------------------------------- backups


@router.get("/backups")
async def backups_page(request: Request) -> Response:
    web = get_web(request)
    items = await web.app.db.run(repo.list_backups)
    return await render(request, "backups.html", page="backups", items=items)


@router.post("/backups/create")
async def backups_create(request: Request) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    try:
        await web.pipeline.create_backup("manual", auth.actor, full=True)
    except (BackupError, OperationRejected) as exc:
        return redirect(request, "/backups", ("err", clean(str(exc))))
    return redirect(request, "/backups", ("ok", T["backup_created"]))


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    for i in range(0, len(data), CHUNK):
        yield data[i : i + CHUNK]


@router.get("/backups/{backup_id}/download")
async def backup_download(request: Request, backup_id: int) -> Response:
    web = get_web(request)
    rec = await web.app.db.run(repo.get_backup, backup_id)
    base = web.pipeline.config.paths.backups_dir.rstrip("/") + "/"
    if rec is None or posixpath.normpath(rec.path) != rec.path or not rec.path.startswith(base):
        raise HttpError(404, T["not_found"])
    try:
        data = await web.pipeline.ops.read_file(rec.path)
    except SystemOpsError:
        raise HttpError(404, T["not_found"]) from None
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(rec.path))
    return StreamingResponse(
        _stream(data),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
        },
    )


@router.post("/backups/{backup_id}/restore")
async def backup_restore(request: Request, backup_id: int) -> Response:
    web = get_web(request)
    auth = auth_of(request)
    form = await load_form(request)
    if fstr(form, "confirm").lower() != T["restore_word"]:
        return redirect(
            request, "/backups", ("err", T["restore_confirm_bad"].format(word=T["restore_word"]))
        )
    try:
        outcome = await web.pipeline.restore_backup(backup_id, auth.actor)
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
    raw_page = request.query_params.get("page", "1")
    if tab not in ("audit", "apply") or not raw_page.isdigit() or not 1 <= int(raw_page) <= 100000:
        raise HttpError(400, T["bad_filter"])
    page = int(raw_page)
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
