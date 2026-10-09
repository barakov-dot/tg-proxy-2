"""Dependencies of the bot, injected into handlers as ``deps``."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.apply.settings_spec import AppSettings
from tgpanel.db.connection import Database
from tgpanel.domain.queries import Period
from tgpanel.services.api import OperationResult, UserService
from tgpanel.services.broadcast import BroadcastService
from tgpanel.services.notifier import MessageSender, Messenger, Templates, scrub_secrets
from tgpanel.services.requests import RequestService

log = logging.getLogger("tgpanel.bot")


class SettingsPort(Protocol):
    """The part of ``SettingsServiceImpl`` the bot uses."""

    async def snapshot(self) -> AppSettings: ...
    async def set(self, key: str, value: object, actor: str) -> OperationResult: ...


class TrafficReader(Protocol):
    """The part of ``services.traffic.TrafficService`` the bot uses.

    ``user_totals`` may return a ``(bytes_up, bytes_down)`` tuple or an object with
    ``bytes_up`` / ``bytes_down`` (or ``up`` / ``down``) attributes; see ``totals_pair``.
    """

    async def user_totals(self, user_id: int, period: Period) -> Any: ...


def totals_pair(value: Any) -> tuple[int, int]:
    if isinstance(value, tuple) and len(value) >= 2:
        return int(value[0]), int(value[1])
    for up, down in (("bytes_up", "bytes_down"), ("up", "down")):
        if hasattr(value, up) and hasattr(value, down):
            return int(getattr(value, up)), int(getattr(value, down))
    raise TypeError("unsupported traffic totals value")


@dataclass
class BotDeps:
    users: UserService
    settings: SettingsPort
    pipeline: ApplyPipeline
    db: Database
    requests: RequestService
    broadcast: BroadcastService
    traffic: TrafficReader
    sender: MessageSender  # real: AiogramSender bound to the Bot
    messenger: Messenger  # the application-wide one (shared rate limiter)
    monotonic: Callable[[], float] = time.monotonic
    throttle_interval: float = 0.4  # seconds between events of one Telegram user
    templates: Templates = field(init=False)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.templates = Templates(self.db)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run ``coro`` in the background, keeping a reference and logging how it ended."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            log.warning("background task was cancelled before it finished")
            return
        exc = task.exception()
        if exc is not None:
            log.warning(
                "background task failed: %s: %s", type(exc).__name__, scrub_secrets(str(exc), 200)
            )

    async def wait_background(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        """Called when the bot stops: unfinished background tasks are logged and cancelled."""
        pending = [t for t in self.tasks if not t.done()]
        if pending:
            log.warning("%d background task(s) unfinished at shutdown", len(pending))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
