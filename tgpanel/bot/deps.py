"""Dependencies of the bot, injected into handlers as ``deps``."""

from __future__ import annotations

import asyncio
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
from tgpanel.services.notifier import MessageSender, Messenger
from tgpanel.services.requests import RequestService


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
    monotonic: Callable[[], float] = time.monotonic
    throttle_interval: float = 0.4  # seconds between events of one Telegram user
    messenger: Messenger = field(init=False)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.messenger = Messenger(self.sender, self.pipeline)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run ``coro`` in the background, keeping a reference until it finishes."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def wait_background(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
