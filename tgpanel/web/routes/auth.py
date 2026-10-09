"""Login / logout."""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response

from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.web.deps import WebContext
from tgpanel.web.routes.common import (
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

# a fixed valid argon2 hash used to spend the same time for unknown / missing accounts
_DUMMY_HASH: str | None = None


async def _audit(web: WebContext, actor: str, action: str, details: str = "") -> None:
    def write(conn: sqlite3.Connection) -> None:
        repo.add_audit(conn, web.now(), actor, action, "", details)

    try:
        await web.pipeline.db_write(write)
    except OperationRejected:
        log.warning("audit write skipped: database busy")


def _verify(web: WebContext, stored_hash: str, password: str) -> bool:
    global _DUMMY_HASH
    if not stored_hash:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = web.password_hasher.hash(secrets.token_urlsafe(8))
        try:
            web.password_hasher.verify(_DUMMY_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return False
    try:
        return bool(web.password_hasher.verify(stored_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def _login_page(request: Request, status: int = 200, error: str | None = None) -> Response:
    web = get_web(request)
    token = get_signer(request).make_login_token(int(web.now().timestamp()))
    response = await render(request, "login.html", status, login_token=token, error=error)
    set_cookie(response, request, LOGIN_COOKIE, token, 3600)
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


@router.post("/login")
async def login_submit(request: Request) -> Response:
    web = get_web(request)
    signer = get_signer(request)
    ip = client_ip(request, web)
    wait = web.limiter.allow(ip)
    if wait > 0:
        response = await _login_page(
            request, 429, T["login_throttled"].format(seconds=int(wait) + 1)
        )
        response.headers["Retry-After"] = str(int(wait) + 1)
        return response
    form = await load_form(request)
    now_s = int(web.now().timestamp())
    if not signer.login_token_ok(
        request.cookies.get(LOGIN_COOKIE), fraw(form, "login_token"), now_s
    ):
        return await _login_page(request, 400, T["login_token_bad"])
    login = fstr(form, "username")
    password = fraw(form, "password")
    cfg = await web.app.db.run(read_panel_auth)
    ok_hash = _verify(web, cfg.password_hash, password) if len(password) <= 1024 else False
    ok_login = hmac.compare_digest(login.encode(), cfg.login.encode()) if cfg.login else False
    if not (ok_hash and ok_login):
        await _audit(web, "web:anonymous", "web.login_failed", f"ip={ip}")
        return await _login_page(request, 401, T["login_failed"])
    web.limiter.success(ip)
    session = signer.make_session(cfg.login, cfg.version, now_s)
    response = redirect(request, "/")
    set_cookie(response, request, SESSION_COOKIE, signer.dump_session(session), SESSION_TTL_S)
    delete_cookie(response, request, LOGIN_COOKIE)
    await _audit(web, f"web:{cfg.login}", "web.login", f"ip={ip}")
    return response


@router.post("/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request) -> Response:
    response = redirect(request, "/login")
    delete_cookie(response, request, SESSION_COOKIE)
    return response
