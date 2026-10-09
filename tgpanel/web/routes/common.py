"""Shared request helpers: auth dependency, rendering, flash messages, form parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Path, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData, UploadFile
from starlette.templating import Jinja2Templates

from tgpanel.db import repo
from tgpanel.system.validation import scrub
from tgpanel.web.deps import WebContext
from tgpanel.web.inputs import MAX_ID, ip_key, parse_uint
from tgpanel.web.security import (
    FLASH_COOKIE,
    SESSION_COOKIE,
    Session,
    Signer,
    csrf_ok,
)
from tgpanel.web.texts import T

PathId = Annotated[int, Path(ge=1, le=MAX_ID)]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
KEY_LOGIN = "panel_login"
KEY_HASH = "panel_password_hash"
KEY_VERSION = "panel_session_version"
MAX_FORM_FIELDS = 20_000
_UNSAFE_TEXT = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")


class LoginRequired(Exception):
    """No valid session."""


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class AuthInfo:
    session: Session

    @property
    def actor(self) -> str:
        return f"web:{self.session.login}"


def auth_of(request: Request) -> AuthInfo:
    auth = request.state.auth
    return cast(AuthInfo, auth)


def get_web(request: Request) -> WebContext:
    web = request.app.state.web
    return cast(WebContext, web)


def get_signer(request: Request) -> Signer:
    signer = request.app.state.signer
    return cast(Signer, signer)


def root_of(request: Request) -> str:
    return str(request.app.state.root_path)


def cookie_path(request: Request) -> str:
    """``/<RANDOM>/`` as in PLAN 5.1 (``/`` for an empty root path)."""
    return root_of(request) + "/"


def clean(text: str | None, limit: int = 500) -> str:
    """Make a service/adapter message safe to show: no secret-looking strings, bounded."""
    return scrub(text or "", limit)


def client_ip(request: Request, web: WebContext) -> str:
    """Limiter key of the client: IPv6 -> /64, IPv4-mapped -> IPv4.

    ``X-Forwarded-For`` is used only when the TCP peer is a trusted proxy, and only its LAST
    entry (the one our proxy appended); a malformed last entry falls back to the peer address.
    """
    peer = request.client.host if request.client else ""
    if peer in web.trusted_proxies:
        entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",")]
        entries = [e for e in entries if e]
        if entries:
            key = ip_key(entries[-1])
            if key is not None:
                return key
    return ip_key(peer) or peer or "unknown"


# ------------------------------------------------------------------------------ cookies


def set_cookie(
    response: Response, request: Request, name: str, value: str, max_age: int | None
) -> None:
    web = get_web(request)
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=cookie_path(request),
        httponly=True,
        secure=web.cookie_secure,
        samesite="strict",
    )


def delete_cookie(response: Response, request: Request, name: str) -> None:
    web = get_web(request)
    response.delete_cookie(
        name,
        path=cookie_path(request),
        httponly=True,
        secure=web.cookie_secure,
        samesite="strict",
    )


def set_flash(response: Response, request: Request, kind: str, text: str) -> None:
    payload = {"k": kind, "m": clean(text, 400)}
    set_cookie(response, request, FLASH_COOKIE, get_signer(request).dumps("flash", payload), 300)


def redirect(request: Request, path: str, flash: tuple[str, str] | None = None) -> Response:
    """303 to ``path`` (relative to the app root). ``flash`` = (``ok``|``err``, text)."""
    response = RedirectResponse(root_of(request) + path, status_code=303)
    if flash is not None:
        set_flash(response, request, *flash)
    return response


# ------------------------------------------------------------------------------- panel auth


@dataclass(frozen=True, slots=True)
class PanelAuthConfig:
    login: str
    password_hash: str
    version: int


def read_panel_auth(conn: Any) -> PanelAuthConfig:
    login = repo.get_setting(conn, KEY_LOGIN, "") or ""
    pw_hash = repo.get_setting(conn, KEY_HASH, "") or ""
    raw = repo.get_setting(conn, KEY_VERSION, "1") or "1"
    version = int(raw) if raw.isascii() and raw.isdigit() else 1
    return PanelAuthConfig(login, pw_hash, version)


async def load_form(request: Request) -> FormData:
    return await request.form(max_fields=MAX_FORM_FIELDS, max_files=4)


async def require_auth(request: Request) -> AuthInfo:
    """Dependency of every protected route: session + panel version + CSRF on unsafe methods."""
    web = get_web(request)
    signer = get_signer(request)
    session = signer.load_session(request.cookies.get(SESSION_COOKIE), int(web.now().timestamp()))
    if session is None:
        raise LoginRequired
    cfg = await web.app.db.run(read_panel_auth)
    if not cfg.password_hash or cfg.login != session.login or cfg.version != session.version:
        raise LoginRequired
    if request.method not in SAFE_METHODS:
        form = await load_form(request)
        sent = form.get("csrf_token")
        header = request.headers.get("x-csrf-token")
        if not csrf_ok(session.csrf, header, sent if isinstance(sent, str) else None):
            raise HttpError(403, T["csrf_failed"])
    auth = AuthInfo(session)
    request.state.auth = auth
    return auth


# -------------------------------------------------------------------------------- forms


def fstr(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else default


def fraw(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key)
    return value if isinstance(value, str) else default


def flist(form: FormData, key: str) -> list[str]:
    return [v for v in form.getlist(key) if isinstance(v, str)]


def fint(form: FormData, key: str) -> int | None:
    raw = fstr(form, key)
    if not raw:
        return None
    value = parse_uint(raw)
    if value is None:
        raise HttpError(400, T["bad_number"])
    return value


def fids(form: FormData, key: str = "ids") -> list[int]:
    out: list[int] = []
    for raw in flist(form, key):
        value = parse_uint(raw, max_digits=12)
        if value is not None and 1 <= value <= MAX_ID:
            out.append(value)
    return list(dict.fromkeys(out))


async def upload_text(form: FormData, key: str, limit: int = 1_000_000) -> str:
    value = form.get(key)
    if isinstance(value, UploadFile):
        data = await value.read(limit + 1)
        if len(data) > limit:
            raise HttpError(400, T["file_too_big"])
        return data.decode("utf-8", "replace")
    return ""


# ---------------------------------------------------------------------------- rendering


async def panel_tz(web: WebContext) -> ZoneInfo:
    name = await web.app.db.run(repo.get_setting, "timezone", "UTC")
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _human_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    n = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


async def banner_state(web: WebContext) -> dict[str, Any]:
    runs = await web.app.db.run(repo.list_apply_runs, 1)
    last = runs[0] if runs else None
    failure = last if last is not None and last.status == "failed" else None
    return {
        "applying": web.pipeline.is_applying,
        "reason": web.pipeline.current_reason,
        "failure": None
        if failure is None
        else {"id": failure.id, "error": clean(failure.error, 300), "reason": failure.reason},
    }


def qurl(path: str, params: dict[str, list[str]] | None = None, **override: Any) -> str:
    """``path`` + query string from ``params`` with ``override`` applied (None removes a key)."""
    merged: dict[str, list[str]] = dict(params or {})
    for key, value in override.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, list | tuple):
            merged[key] = [str(v) for v in value]
        else:
            merged[key] = [str(value)]
    pairs = [(k, v) for k, vs in merged.items() for v in vs]
    return path + ("?" + urlencode(pairs) if pairs else "")


async def render(
    request: Request,
    name: str,
    status: int = 200,
    *,
    page: str = "",
    **context: Any,
) -> Response:
    web = get_web(request)
    templates: Jinja2Templates = request.app.state.templates
    root = root_of(request)
    auth: AuthInfo | None = getattr(request.state, "auth", None)
    tz = await panel_tz(web)

    def fmt(dt: datetime | None, with_time: bool = True) -> str:
        if dt is None:
            return "—"
        local = dt.astimezone(tz)
        return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")

    def url(path: str) -> str:
        return root + path

    fragment = name.startswith("_")
    base: dict[str, Any] = {
        "t": T,
        "root": root,
        "url": url,
        "fmt": fmt,
        "qurl": lambda p, params=None, **o: url(qurl(p, params, **o)),
        "human_bytes": _human_bytes,
        "csrf": auth.session.csrf if auth else "",
        "login": auth.session.login if auth else "",
        "page": page,
        "tz_name": str(tz),
        "banner": None,
        "flash": None,
        "now_utc": datetime.now(UTC),
    }
    flash_value: dict[str, Any] | None = None
    if auth is not None and not fragment:
        base["banner"] = await banner_state(web)
        flash_value = get_signer(request).loads("flash", request.cookies.get(FLASH_COOKIE))
        if flash_value is not None:
            base["flash"] = {"kind": str(flash_value.get("k", "ok")), "text": flash_value.get("m")}
    base.update(context)
    response = templates.TemplateResponse(request, name, base, status_code=status)
    if flash_value is not None:
        delete_cookie(response, request, FLASH_COOKIE)
    return response
