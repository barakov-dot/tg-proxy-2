"""ASGI guards: request body size cap and Host header allow-list."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tgpanel.web.routes.common import HttpError
from tgpanel.web.texts import T

MAX_BODY_BYTES = 2 * 1024 * 1024
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
HOSTS_TTL_S = 30.0


def host_of(header: str) -> str:
    """Host name without the port (IPv6 literals keep their brackets)."""
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


class BodyLimit:
    """413 for bodies over ``limit`` bytes: by Content-Length, and while streaming."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isascii() or not value.isdigit() or int(value) > self.limit:
                    await PlainTextResponse(T["body_too_big"], status_code=413)(
                        scope, receive, send
                    )
                    return
        total = 0

        async def limited() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit:
                    raise HttpError(413, T["body_too_big"])
            return message

        await self.app(scope, limited, send)


class HostGuard:
    """Only the panel host name (from the settings) and loopback names are served."""

    def __init__(
        self,
        app: ASGIApp,
        allowed: Callable[[], Awaitable[set[str]]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._allowed = allowed
        self._mono = monotonic
        self._cache: tuple[float, set[str]] | None = None

    async def _hosts(self) -> set[str]:
        now = self._mono()
        if self._cache is None or now - self._cache[0] > HOSTS_TTL_S:
            self._cache = (now, await self._allowed())
        return self._cache[1]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = ""
        for name, value in scope["headers"]:
            if name == b"host":
                host = host_of(value.decode("latin-1"))
        if host in LOCAL_HOSTS or host in await self._hosts():
            await self.app(scope, receive, send)
            return
        response: Any = PlainTextResponse("Invalid host", status_code=400)
        await response(scope, receive, send)
