"""One place that wires bot, scheduler, requests and broadcasts (the orchestrator only composes).

Usage in main::

    rt = build_runtime(ctx, traffic=TrafficService(ctx.db), maybe_rollup=...)
    # web: WebContext(requests=rt.requests_port, broadcast=rt.broadcast_port, ...)
    tasks = [rt.bot_task(stop), rt.scheduler_task(stop)]
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from aiogram.client.session.base import BaseSession

from tgpanel.bot.app import run_bot
from tgpanel.bot.broadcast_port import BroadcastPortAdapter, RequestsPortAdapter
from tgpanel.bot.deps import BotDeps, TrafficReader
from tgpanel.bot.sender import AiogramSender, BotNotifier
from tgpanel.db import repo
from tgpanel.scheduler.scheduler import Scheduler
from tgpanel.services.broadcast import BroadcastService, LinkDelivery
from tgpanel.services.container import AppContext
from tgpanel.services.notifier import (
    LateBoundNotifier,
    Messenger,
    RateLimiter,
    make_on_failure,
    scrub_secrets,
)
from tgpanel.services.requests import RequestService

log = logging.getLogger("tgpanel.bot")
KEY_BOT_TOKEN = "bot_token"  # noqa: S105 - setting name, not a secret
MESSAGES_PER_SECOND = 20.0


@dataclass
class Runtime:
    ctx: AppContext
    sender: AiogramSender
    notifier: LateBoundNotifier
    messenger: Messenger
    requests: RequestService  # ONE instance shared by the bot and the web panel
    broadcast: BroadcastService
    link_delivery: LinkDelivery
    deps: BotDeps
    scheduler: Scheduler
    broadcast_port: BroadcastPortAdapter  # for WebContext.broadcast
    requests_port: RequestsPortAdapter  # for WebContext.requests
    token: str | None
    session: BaseSession | None
    ready_timeout_s: float

    async def _token(self) -> str | None:
        if self.token:
            return self.token
        stored = await self.ctx.db.run(repo.get_setting, KEY_BOT_TOKEN, "")
        return str(stored or "").strip() or None

    async def bot_task(self, stop_event: asyncio.Event) -> None:
        """Long polling until ``stop_event``; without a token it just waits (logged)."""
        token = await self._token()
        if token is None:
            log.info("bot token is not set: the bot is not started")
            await stop_event.wait()
            return
        try:
            await run_bot(token, self.deps, self.sender, stop_event, session=self.session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("bot stopped: %s: %s", type(exc).__name__, scrub_secrets(str(exc), 200))

    async def scheduler_task(self, stop_event: asyncio.Event) -> None:
        """Waits (bounded) for the bot to be bound, then runs the scheduler loop."""
        if await self._token() is not None:
            await self.sender.wait_ready(self.ready_timeout_s)
        await self.scheduler.run(stop_event)


def build_runtime(
    ctx: AppContext,
    *,
    traffic: TrafficReader,
    token: str | None = None,
    maybe_rollup: Callable[[datetime], Awaitable[object]] | None = None,
    rate: float = MESSAGES_PER_SECOND,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    session: BaseSession | None = None,
    throttle_interval: float = 0.4,
    ready_timeout_s: float = 30.0,
    tick_s: float = 60.0,
) -> Runtime:
    """``token=None`` reads the ``bot_token`` setting when the bot starts.

    Also installs the apply-failure alert on the pipeline unless one is already set.
    """
    sender = AiogramSender()
    notifier = LateBoundNotifier()
    notifier.bind(BotNotifier(sender, ctx.db))
    if ctx.pipeline.on_failure is None:
        ctx.pipeline.on_failure = make_on_failure(notifier)
    # ONE limiter for everything that sends: broadcasts, notices, reminders, web "send link".
    messenger = Messenger(
        sender, ctx.pipeline, limiter=RateLimiter(rate, clock=monotonic, sleep=sleep), sleep=sleep
    )
    requests = RequestService(ctx.pipeline, ctx.db, ctx.users)
    broadcast = BroadcastService(ctx.pipeline, ctx.db, ctx.users, sender, messenger=messenger)
    link_delivery = LinkDelivery(ctx.pipeline, ctx.db, ctx.users, messenger, sleep=sleep)
    deps = BotDeps(
        users=ctx.users,
        settings=ctx.settings,
        pipeline=ctx.pipeline,
        db=ctx.db,
        requests=requests,
        broadcast=broadcast,
        traffic=traffic,
        sender=sender,
        messenger=messenger,
        link_delivery=link_delivery,
        sleep=sleep,
        monotonic=monotonic,
        throttle_interval=throttle_interval,
    )
    scheduler = Scheduler(
        users=ctx.users,
        pipeline=ctx.pipeline,
        db=ctx.db,
        messenger=messenger,
        notifier=notifier,
        maybe_rollup=maybe_rollup,
        ready=lambda: sender.is_bound,
        clock=clock,
        sleep=sleep,
        tick_s=tick_s,
    )
    return Runtime(
        ctx=ctx,
        sender=sender,
        notifier=notifier,
        messenger=messenger,
        requests=requests,
        broadcast=broadcast,
        link_delivery=link_delivery,
        deps=deps,
        scheduler=scheduler,
        broadcast_port=BroadcastPortAdapter(
            broadcast, ctx.users, ctx.db, ctx.pipeline, messenger, link_delivery
        ),
        requests_port=RequestsPortAdapter(requests, ctx.users, ctx.db, messenger, link_delivery),
        token=token,
        session=session,
        ready_timeout_s=ready_timeout_s,
    )
