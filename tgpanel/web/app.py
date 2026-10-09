"""FastAPI application factory of the panel.

Runner notes (orchestrator): bind ONLY to 127.0.0.1, run uvicorn with ``access_log=False`` (a
URL may carry ids; no access logging anywhere), ``proxy_headers=False`` (we parse
``X-Forwarded-For`` ourselves, only from trusted proxies) and set ``TGPANEL_SECRET_KEY``.
The reverse proxy does NOT strip the random prefix; the app accepts requests both with and
without it (Starlette strips ``root_path`` when present) and generates every URL, redirect and
cookie path with the prefix.
"""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

from tgpanel.db import repo
from tgpanel.system.validation import scrub
from tgpanel.web.deps import WebContext
from tgpanel.web.guards import BodyLimit, HostGuard
from tgpanel.web.routes import admin, auth, create, dashboard, imports, users
from tgpanel.web.routes.common import HttpError, LoginRequired, get_web, render, root_of
from tgpanel.web.security import Signer
from tgpanel.web.texts import T

log = logging.getLogger("tgpanel.web")

BASE = Path(__file__).parent
_ROOT_RE = re.compile(r"^(/[A-Za-z0-9_-]+)*$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def make_templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(BASE / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return Jinja2Templates(env=env)


def create_app(ctx: WebContext, root_path: str) -> FastAPI:
    root = root_path.rstrip("/")
    if not _ROOT_RE.match(root):
        raise ValueError("root_path must look like /<random>")
    app = FastAPI(
        root_path=root,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web = ctx
    app.state.signer = Signer(ctx.secret_key)
    app.state.root_path = root
    app.state.templates = make_templates()
    app.state.failure_audit = {}

    async def allowed_hosts() -> set[str]:
        name = await ctx.app.db.run(repo.get_setting, "panel_hostname", "")
        return {h.lower() for h in ctx.extra_hosts} | ({name.lower()} if name else set())

    app.add_middleware(BodyLimit)
    app.add_middleware(HostGuard, allowed=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            summary = scrub(f"{type(exc).__name__}: {exc}", 300)
            trace = scrub("".join(traceback.format_exception(exc)), 6000)
            log.error("unhandled error: %s\n%s", summary, trace)
            response = await _error_page(request, 500, T["server_error"])
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        path = request.scope.get("path", "")
        if "/static/" in path and response.status_code == 200:
            h.setdefault("Cache-Control", "public, max-age=86400")
        else:
            h["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        target = root_of(request) + "/login"
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        from fastapi.responses import RedirectResponse

        return RedirectResponse(target, status_code=303)

    async def _error_page(request: Request, status: int, message: str) -> Response:
        if request.headers.get("hx-request"):
            return Response(
                f'<div class="alert err">{_esc(message)}</div>',
                status_code=status,
                media_type="text/html",
            )
        if "application/json" in request.headers.get("accept", "") and "json" in request.url.path:
            return JSONResponse({"error": message}, status_code=status)
        return await render(request, "error.html", status, message=message, code=status)

    @app.exception_handler(HttpError)
    async def _http_error(request: Request, exc: HttpError) -> Response:
        return await _error_page(request, exc.status, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = T["not_found"] if exc.status_code == 404 else T["http_error"]
        return await _error_page(request, exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        return await _error_page(request, 400, T["bad_request"])

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        # unauthenticated liveness probe: answers only that the process serves requests
        return JSONResponse({"ok": True})

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(create.router)  # before users: /users/new vs /users/{id}
    app.include_router(users.router)
    app.include_router(imports.router)
    app.include_router(admin.router)
    return app


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


__all__ = ["create_app", "get_web"]
