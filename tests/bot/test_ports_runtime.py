# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
from datetime import timedelta

from tests.apply.conftest import SECRET_RE
from tests.bot.conftest import ADMIN, USER, Env
from tests.bot.helpers import TOKEN, FakeTraffic, MockSession, forbidden
from tgpanel.bot.broadcast_port import BroadcastPortAdapter, RequestsPortAdapter
from tgpanel.bot.runtime import build_runtime
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.web import deps as web_deps


def port(env: Env) -> BroadcastPortAdapter:
    return BroadcastPortAdapter(
        env.deps.broadcast,
        env.svc.users,
        env.svc.ctx.db,
        env.svc.ctx.pipeline,
        env.deps.messenger,
        env.deps.link_delivery,
    )


# ------------------------------------------------------------------ BroadcastPort


async def test_preview_has_no_links_and_matches_web_dto(env: Env) -> None:
    ok = await env.make_user("Alice", tg_id=USER, started=True)
    nobot = await env.make_user("NoBot", tg_id=USER + 1)
    notg = await env.make_user("NoTg")
    ids = [ok.id, nobot.id, notg.id]
    pv = await port(env).preview("Привет, {name}! {link} {tg_link} до {expires} <i>", ids)
    assert isinstance(pv, web_deps.BroadcastPreview)
    assert (pv.recipients, pv.sendable, pv.skipped, pv.error) == (3, 1, 2, None)
    assert pv.sample.startswith("Привет, Alice! [ссылка] [ссылка] до ")
    assert "<i>" in pv.sample  # plain text for the web template engine to escape
    assert ok.secret not in pv.sample and not SECRET_RE.search(pv.sample)
    assert "t.me" not in pv.sample
    assert env.sender.sent == []


async def test_preview_errors_and_empty(env: Env) -> None:
    p = port(env)
    bad = await p.preview("   ", [])
    assert bad.error and bad.recipients == 0
    empty = await p.preview("hi", [])
    assert (empty.recipients, empty.sendable, empty.sample) == (0, 0, "")


async def test_start_and_report_lifecycle(env: Env) -> None:
    a = await env.make_user("A", tg_id=USER, started=True)
    b = await env.make_user("B", tg_id=USER + 1)
    p = port(env)
    bid = await p.start("hi {name}", [a.id, b.id], "web:admin")
    await p.wait_background()
    rep = await p.report(bid)
    assert isinstance(rep, web_deps.BroadcastReport)
    assert (rep.status, rep.total, rep.sent, rep.failed, rep.skipped) == ("done", 2, 1, 0, 1)
    by_user = {i.user_id: i for i in rep.items}
    assert isinstance(next(iter(rep.items)), web_deps.BroadcastItem)
    assert by_user[a.id].result == "sent"
    assert by_user[a.id + 1].result == "skipped" and "бот не запущен" in by_user[a.id + 1].note
    assert [s.chat_id for s in env.sender.sent] == [USER]
    assert await p.report(999) is None


async def test_report_failed_blocked_and_running(env: Env) -> None:
    await env.make_user("A", tg_id=USER, started=True)
    await env.make_user("B", tg_id=USER + 1, started=True)
    env.sender.script[USER] = [forbidden(USER)]
    env.sender.script[USER + 1] = [RuntimeError("net")]
    p = port(env)
    bid = await env.deps.broadcast.create("hi", "web:admin")
    first = await p.report(bid)
    assert first and first.status == "running" and all(i.result == "pending" for i in first.items)
    await env.deps.broadcast.run(bid)
    rep = await p.report(bid)
    assert rep and rep.status == "done" and rep.failed == 2 and rep.sent == 0
    assert sorted(i.result for i in rep.items) == ["blocked", "failed"]


async def test_send_links_results(env: Env) -> None:
    ok = await env.make_user("Ok", tg_id=USER, started=True)
    nobot = await env.make_user("NoBot", tg_id=USER + 1)
    notg = await env.make_user("NoTg")
    blocked = await env.make_user("Blk", tg_id=USER + 2, started=True)
    env.sender.script[USER + 2] = [forbidden(USER + 2)]
    out = await port(env).send_links([ok.id, nobot.id, notg.id, blocked.id, 9999], "web:admin")
    assert all(isinstance(r, web_deps.LinkSendResult) for r in out)
    assert [r.ok for r in out] == [True, False, False, False, False]
    assert "не запущен" in out[1].note and "Telegram ID" in out[2].note
    assert "заблокировал" in out[3].note and "не найден" in out[4].note
    sent = env.sender.to(USER)[0]
    assert sent.button and sent.button.url == env.svc.users.link(ok)
    extra = env.svc.ctx.db.call(repo.get_user_extra, blocked.id)
    assert extra and not extra.can_message
    assert "user.send_links" in env.svc.audit_text()
    assert ok.secret not in env.svc.audit_text()


# ------------------------------------------------------------------ RequestsPort


async def test_requests_port_approve_and_reject_notify_requester(env: Env) -> None:
    env.set_setting("issuance_mode", "approval")
    rp = RequestsPortAdapter(
        env.deps.requests, env.svc.users, env.svc.ctx.db, env.deps.messenger, env.deps.link_delivery
    )
    await env.tg.press(USER, "req")
    await env.tg.press(USER + 1, "req")
    res = await rp.approve(1, Term.MONTH, "web:admin")
    assert res == web_deps.DecisionResult(True)
    user = env.svc.ctx.db.call(repo.get_user_by_tg_id, USER)
    assert user
    note = env.sender.to(USER)[0]
    assert note.button and note.button.url == env.svc.users.link(user)
    again = await rp.approve(1, None, "web:admin")
    assert not again.ok and "уже обработана" in (again.error or "")
    assert (await rp.reject(2, "web:admin")).ok
    assert "отклонена" in env.sender.to(USER + 1)[0].text
    assert not (await rp.reject(77, "web:admin")).ok


# ------------------------------------------------------------------ runtime


async def test_build_runtime_wires_shared_instances(env: Env) -> None:
    ctx = env.svc.ctx
    ctx.pipeline.on_failure = None
    rt = build_runtime(ctx, traffic=FakeTraffic(), token=TOKEN, session=MockSession())
    assert rt.deps.requests is rt.requests
    assert rt.requests_port._requests is rt.requests  # ONE RequestService for bot and web
    assert rt.broadcast._messenger is rt.messenger is rt.deps.messenger
    assert rt.broadcast_port._messenger is rt.messenger
    assert rt.scheduler._messenger is rt.messenger
    assert ctx.pipeline.on_failure is not None
    assert not rt.scheduler._is_ready()
    keep = object()
    ctx.pipeline.on_failure = keep
    build_runtime(ctx, traffic=FakeTraffic())
    assert ctx.pipeline.on_failure is keep  # an existing hook is not replaced


async def test_runtime_tasks_start_and_stop(env: Env) -> None:
    session = MockSession()
    rt = build_runtime(
        env.svc.ctx, traffic=FakeTraffic(), token=TOKEN, session=session, tick_s=0.01
    )
    stop = asyncio.Event()
    bot = asyncio.create_task(rt.bot_task(stop))
    sched = asyncio.create_task(rt.scheduler_task(stop))
    for _ in range(100):
        if rt.sender.is_bound:
            break
        await asyncio.sleep(0.01)
    assert rt.sender.is_bound and rt.scheduler._is_ready()
    stop.set()
    await asyncio.wait_for(asyncio.gather(bot, sched), timeout=5)
    assert not rt.sender.is_bound
    assert any(n == "GetUpdates" for n, _ in session.calls)


async def test_runtime_without_token_waits_for_stop(env: Env) -> None:
    rt = build_runtime(env.svc.ctx, traffic=FakeTraffic(), tick_s=0.01)
    stop = asyncio.Event()
    bot = asyncio.create_task(rt.bot_task(stop))
    sched = asyncio.create_task(rt.scheduler_task(stop))
    await asyncio.sleep(0.05)
    assert not bot.done() and not rt.sender.is_bound
    stop.set()
    await asyncio.wait_for(asyncio.gather(bot, sched), timeout=5)


async def test_runtime_token_from_settings(env: Env) -> None:
    env.set_setting("bot_token", TOKEN)
    rt = build_runtime(env.svc.ctx, traffic=FakeTraffic(), session=MockSession())
    assert await rt._token() == TOKEN


async def test_apply_failure_alert_goes_to_admins_through_runtime(env: Env) -> None:
    ctx = env.svc.ctx
    ctx.pipeline.on_failure = None
    session = MockSession()
    rt = build_runtime(ctx, traffic=FakeTraffic(), token=TOKEN, session=session)
    from aiogram import Bot

    rt.sender.bind(Bot(TOKEN, session=session))  # as run_bot does
    assert ctx.pipeline.on_failure is not None
    from tgpanel.apply.pipeline import ApplyFailure

    await ctx.pipeline.on_failure(ApplyFailure(3, "create", "web:admin", "boom", True, ()))
    assert any("Не удалось применить" in t for t in session.texts(ADMIN))
    _ = timedelta
