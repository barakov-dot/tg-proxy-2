"""Web-layer dependencies: the context object and the adapter protocols to other services.

The orchestrator wires the real services behind ``TrafficPort`` / ``RequestsPort`` /
``BroadcastPort`` (thin adapters); the web layer never imports the bot, scheduler or collector.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from argon2 import PasswordHasher

from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.domain.expiry import Term
from tgpanel.domain.queries import Period
from tgpanel.services.admin import AdminService, RestartBot, WriteEnv
from tgpanel.services.api import OperationResult
from tgpanel.services.backups import BackupOpener, BackupService
from tgpanel.services.bulk import BulkService
from tgpanel.services.container import AppContext
from tgpanel.services.dashboard import DashboardView, PoolView
from tgpanel.services.server_metrics import MetricsView
from tgpanel.web.security import GlobalFailureLimiter, LoginLimiter

log = logging.getLogger("tgpanel.web")

# --------------------------------------------------------------------------- traffic port


@dataclass(frozen=True, slots=True)
class Totals:
    up: int
    down: int


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """``points`` items are ``(ts, up, down)`` tuples or objects with ts/up/down attributes;
    ``ts`` is an aware datetime or a unix timestamp in seconds."""

    granularity: str  # "minute" | "hour" | "day"
    points: Sequence[Any]
    total_up: int
    total_down: int


class TrafficPort(Protocol):
    async def user_series(
        self, user_id: int, start: datetime | None, end: datetime, max_points: int
    ) -> SeriesResult: ...
    async def global_series(
        self, start: datetime, end: datetime, max_points: int
    ) -> SeriesResult: ...
    async def user_totals(self, user_id: int, period: Period) -> Totals: ...
    async def dashboard(self) -> DashboardView: ...


class MetricsPort(Protocol):
    async def snapshot(self) -> MetricsView: ...


# ------------------------------------------------------------------------- requests port


@dataclass(frozen=True, slots=True)
class DecisionResult:
    ok: bool
    error: str | None = None  # Russian, secret-free


class RequestsPort(Protocol):
    async def approve(self, request_id: int, term: Term | None, actor: str) -> DecisionResult: ...
    async def reject(self, request_id: int, actor: str) -> DecisionResult: ...


# ------------------------------------------------------------------------ broadcast port


@dataclass(frozen=True, slots=True)
class BroadcastPreview:
    recipients: int  # all selected
    sendable: int  # bot started and can_message
    skipped: int  # excluded (bot not started / blocked)
    sample: str  # rendered text for the first recipient WITHOUT any link/secret
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BroadcastItem:
    user_id: int | None
    tg_id: int | None
    result: str  # pending | sent | failed | blocked | skipped
    note: str = ""


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    broadcast_id: int
    status: str  # running | done
    total: int
    sent: int
    failed: int
    skipped: int
    items: Sequence[BroadcastItem] = ()


@dataclass(frozen=True, slots=True)
class LinkSendResult:
    user_id: int
    ok: bool
    note: str = ""  # Russian reason when not ok


class BroadcastPort(Protocol):
    async def preview(self, template: str, user_ids: Sequence[int]) -> BroadcastPreview: ...
    async def start(self, template: str, user_ids: Sequence[int], actor: str) -> int: ...
    async def report(self, broadcast_id: int) -> BroadcastReport | None: ...
    async def send_links(self, user_ids: Sequence[int], actor: str) -> list[LinkSendResult]: ...


# --------------------------------------------------------------------------------- context


@dataclass
class WebContext:
    app: AppContext
    traffic: TrafficPort
    requests: RequestsPort
    broadcast: BroadcastPort
    secret_key: str  # TGPANEL_SECRET_KEY, >= 32 chars
    cookie_secure: bool = True
    trusted_proxies: frozenset[str] = frozenset({"127.0.0.1", "::1"})
    password_hasher: PasswordHasher = field(default_factory=PasswordHasher)
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    clock: Callable[[], datetime] | None = None  # defaults to the pipeline clock
    global_limiter: GlobalFailureLimiter = field(default_factory=GlobalFailureLimiter)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # login delay (tests inject)
    metrics: MetricsPort | None = None  # server metrics (CPU/memory/disk/network)
    restart_bot: RestartBot | None = None  # restarts only the bot task; True if it will do
    backup_opener: BackupOpener | None = None
    write_env: WriteEnv | None = None  # writes TGPANEL_BOT_TOKEN to the env file (0600, atomic)
    extra_hosts: frozenset[str] = frozenset()  # allowed Host values besides the panel hostname
    admin: AdminService = field(init=False)
    backups: BackupService = field(init=False)
    bulk: BulkService = field(init=False)
    used_tokens: set[str] = field(default_factory=set)  # one-time form tokens already spent

    def __post_init__(self) -> None:
        self.admin = AdminService(
            self.app.pipeline,
            write_env=self.write_env,
            hasher=self.password_hasher,
            restart_bot=self.restart_bot,
        )
        self.backups = BackupService(self.app.pipeline, opener=self.backup_opener)
        self.bulk = BulkService(self.app.pipeline)

    @property
    def pipeline(self) -> ApplyPipeline:
        return self.app.pipeline

    def limiter_clock(self) -> float:
        return self.now().timestamp()

    def now(self) -> datetime:
        return (self.clock or self.app.pipeline.now)()


async def safe[T](call: Awaitable[T], what: str) -> T | None:
    """Await an adapter call; a failure degrades the page instead of breaking it."""
    try:
        return await call
    except Exception as exc:
        log.warning("adapter call failed: %s (%s)", what, type(exc).__name__)
        return None


__all__ = [
    "BroadcastItem",
    "BroadcastPort",
    "BroadcastPreview",
    "BroadcastReport",
    "DashboardView",
    "DecisionResult",
    "LinkSendResult",
    "MetricsPort",
    "MetricsView",
    "OperationResult",
    "PoolView",
    "RequestsPort",
    "SeriesResult",
    "Totals",
    "TrafficPort",
    "WebContext",
    "safe",
]
