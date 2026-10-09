"""Login / logout."""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import APIRouter, Depends, Request, Response

from tgpanel.apply.errors import OperationRejected
from tgpanel.web.deps import WebContext
from tgpanel.web.routes.common import (
    auth_of,
    client_ip,
    delete_cookie,
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
from tgpanel.web.security import LOGIN_COOKIE, SESSION_COOKIE, SESSION_TTL_S
from tgpanel.web.texts import T

router = APIRouter()
log = logging.getLogger("tgpanel.web")

# a valid argon2 hash of a random password: unknown / missing accounts spend the same time
_DUMMY: dict[str, str] = {}
AUDIT_FAILURE_INTERVAL_S = 60.0


async def _audit(web: WebContext, actor: str, action: str, details: str = "") -> None:
    try:
        await web.admin.audit(actor, action, "", details)
    except OperationRejected:
        log.warning("audit write skipped: database busy")


async def _audit_failure(request: Request, web: WebContext, key: str, *, blocked: bool) -> None:
    """One audit row per client per minute (plus one when a block starts), not one per attempt."""
    seen: dict[str, float] = request.app.state.failure_audit
    now = web.limiter_clock()
    if len(seen) > 4096:
        seen.clear()
    last = seen.get(key)
    if blocked:
        seen[key] = now
        await _audit(web, "web:anonymous", "web.login_blocked", f"ip={key}")
    elif last is None or now - last >= AUDIT_FAILURE_INTERVAL_S:
        seen[key] = now
        await _audit(web, "web:anonymous", "web.login_failed", f"ip={key}")


async def _verify(web: WebContext, stored_hash: str, password: str) -> bool:
    if not stored_hash:
        if "hash" not in _DUMMY:
            _DUMMY["hash"] = await web.admin.hash(secrets.token_urlsafe(8))
        await web.admin.verify(_DUMMY["hash"], password)
        return False
    return bool(await web.admin.verify(stored_hash, password))


async def _login_page(request: Request, status: int = 200, error: str | None = None) -> Response:
    web = get_web(request)
    token = get_signer(request).make_login_token(int(web.now().timestamp()))
    response = await render(request, "login.html", status, login_token=token, error=error)
    set_cookie(response, request, LOGIN_COOKIE, token, 3600)
    return response


async def _throttled(seconds: float, request: Request) -> Response:
    response = await _login_page(
        request, 429, T["login_throttled"].format(seconds=int(seconds) + 1)
    )
    response.headers["Retry-After"] = str(int(seconds) + 1)
    return response


@router.get("/login")
async def login_form(request: Request) -> Response:
    web = get_web(request)
    session = get_signer(request).load_session(
        request.cookies.get(SESSION_COOKIE), int(web.now().timestamp())
    )
    if session is not None:
        cfg = await web.app.db.run(read_panel_auth)
        if cfg.password_hash and cfg.login == session.login and cfg.version == session.version:
            return redirect(request, "/")
    return await _login_page(request)


async def _valid_session(request: Request, web: WebContext) -> bool:
    session = get_signer(request).load_session(
        request.cookies.get(SESSION_COOKIE), int(web.now().timestamp())
    )
    if session is None:
        return False
    cfg = await web.app.db.run(read_panel_auth)
    return bool(cfg.password_hash) and cfg.login == session.login and cfg.version == session.version


@router.post("/login")
async def login_submit(request: Request) -> Response:
    web = get_web(request)
    signer = get_signer(request)
    if await _valid_session(request, web):
        return redirect(request, "/")  # an authenticated client is never throttled
    ip = client_ip(request, web)
    wait = web.limiter.allow(ip)
    if wait > 0:
        if web.limiter.newly_blocked:
            await _audit_failure(request, web, ip, blocked=True)
        return await _throttled(wait, request)
    delay = web.global_limiter.delay()
    if delay > 0:
        await web.sleep(delay)  # many failures overall: slow down, never lock anybody out
    form = await load_form(request)
    now_s = int(web.now().timestamp())
    if not signer.login_token_ok(
        request.cookies.get(LOGIN_COOKIE), fraw(form, "login_token"), now_s
    ):
        return await _login_page(request, 400, T["login_token_bad"])
    login = fstr(form, "username")
    password = fraw(form, "password")
    cfg = await web.app.db.run(read_panel_auth)
    ok_hash = await _verify(web, cfg.password_hash, password) if len(password) <= 1024 else False
    ok_login = hmac.compare_digest(login.encode(), cfg.login.encode()) if cfg.login else False
    if not (ok_hash and ok_login):
        if web.global_limiter.record_failure():
            await _audit(web, "web:anonymous", "web.login_flood", "global failure limit reached")
        await _audit_failure(request, web, ip, blocked=False)
        return await _login_page(request, 401, T["login_failed"])
    web.limiter.success(ip)
    try:
        await web.admin.rehash_if_needed(cfg.password_hash, password)
    except OperationRejected:
        log.warning("password rehash skipped: database busy")
    session = signer.make_session(cfg.login, cfg.version, now_s)
    response = redirect(request, "/")
    set_cookie(response, request, SESSION_COOKIE, signer.dump_session(session), SESSION_TTL_S)
    delete_cookie(response, request, LOGIN_COOKIE)
    await _audit(web, f"web:{cfg.login}", "web.login", f"ip={ip}")
    return response


@router.post("/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request) -> Response:
    """Delete this browser's cookie; other sessions end only if "all sessions" is ticked."""
    form = await load_form(request)
    if fstr(form, "all_sessions") == "1":
        await get_web(request).admin.logout_all(auth_of(request).actor)
    response = redirect(request, "/login")
    delete_cookie(response, request, SESSION_COOKIE)
    return response
