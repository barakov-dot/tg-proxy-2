from __future__ import annotations

import html
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from aiogram import Bot, Dispatcher

from tests.bot.helpers import TOKEN, FakeNotifier, FakeSender, FakeTraffic, MockSession, Tg
from tests.services.conftest import Svc, svc  # noqa: F401 - fixture re-export
from tgpanel.bot.app import build_dispatcher, create_bot
from tgpanel.bot.deps import BotDeps
from tgpanel.db import repo
from tgpanel.domain.models import UserRecord
from tgpanel.services.api import NewUser
from tgpanel.services.broadcast import BroadcastService, LinkDelivery
from tgpanel.services.notifier import Messenger, RateLimiter, make_on_failure
from tgpanel.services.requests import RequestService

ADMIN = 1
USER = 500


@dataclass
class Env:
    svc: Svc
    deps: BotDeps
    tg: Tg
    session: MockSession
    sender: FakeSender
    notifier: FakeNotifier
    traffic: FakeTraffic

    async def make_user(
        self,
        name: str = "Vasya",
        tg_id: int | None = None,
        *,
        started: bool = False,
        display_name: str = "",
    ) -> UserRecord:
        res = await self.svc.users.create(
            [NewUser(name=name, tg_id=tg_id, display_name=display_name)], "web:admin"
        )
        assert res.ok, res.error
        uid = res.user_ids[0]
        if started:
            self.svc.ctx.db.call(repo.update_user, uid, bot_started=True, can_message=True)
        user = await self.svc.users.get(uid)
        assert user is not None
        return user

    def link_html(self, user: UserRecord) -> str:
        """The link as it appears inside an HTML-mode message."""
        return html.escape(self.svc.users.link(user), quote=False)

    def set_setting(self, key: str, value: str) -> None:
        self.svc.ctx.db.call(repo.set_setting, key, value)


@pytest.fixture
async def env(svc: Svc) -> AsyncIterator[Env]:  # noqa: F811
    ctx = svc.ctx
    ctx.db.call(repo.add_admin, ADMIN, svc.clock())
    await ctx.users.load_hostname()
    sender = FakeSender()
    notifier = FakeNotifier()
    ctx.pipeline.on_failure = make_on_failure(notifier)
    messenger = Messenger(
        sender,
        ctx.pipeline,
        limiter=RateLimiter(20, clock=sender.time.monotonic, sleep=sender.time.sleep),
        sleep=sender.time.sleep,
    )
    link_delivery = LinkDelivery(
        ctx.pipeline, ctx.db, ctx.users, messenger, sleep=sender.time.sleep
    )
    deps = BotDeps(
        users=ctx.users,
        settings=ctx.settings,
        pipeline=ctx.pipeline,
        db=ctx.db,
        requests=RequestService(ctx.pipeline, ctx.db, ctx.users),
        broadcast=BroadcastService(
            ctx.pipeline,
            ctx.db,
            ctx.users,
            sender,
            messenger=messenger,
        ),
        traffic=FakeTraffic(),
        sender=sender,
        messenger=messenger,
        link_delivery=link_delivery,
        sleep=sender.time.sleep,
        throttle_interval=0.0,
    )
    session = MockSession()
    bot: Bot = create_bot(TOKEN, session)
    dp: Dispatcher = build_dispatcher(deps)
    yield Env(svc, deps, Tg(dp, bot, session), session, sender, notifier, deps.traffic)  # type: ignore[arg-type]
