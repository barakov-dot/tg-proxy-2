# ruff: noqa: RUF001
from __future__ import annotations

import asyncio

import pytest

from tests.bot.helpers import FakeNotifier, FakeSender, forbidden, retry_after
from tests.services.conftest import Svc
from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyFailure
from tgpanel.db import repo
from tgpanel.services.api import NewUser
from tgpanel.services.broadcast import BroadcastService, render_template
from tgpanel.services.notifier import (
    LateBoundNotifier,
    Messenger,
    RateLimiter,
    make_on_failure,
)


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
async def bs(svc: Svc, sender: FakeSender) -> BroadcastService:
    await svc.users.load_hostname()
    return BroadcastService(
        svc.ctx.pipeline,
        svc.ctx.db,
        svc.users,
        sender,
        monotonic=sender.time.monotonic,
        sleep=sender.time.sleep,
    )


async def make(svc: Svc, n: int, *, started: bool = True, base: int = 1000) -> list[int]:
    res = await svc.users.create(
        [NewUser(name=f"u{base + i}", tg_id=base + i) for i in range(n)], "web:admin"
    )
    assert res.ok
    for uid in res.user_ids:
        svc.ctx.db.call(repo.update_user, uid, bot_started=started, can_message=started)
    return list(res.user_ids)


async def test_preview_exclusions_with_reasons(svc: Svc, bs: BroadcastService) -> None:
    ok = await make(svc, 1)
    nobot = await make(svc, 1, started=False, base=2000)
    res = await svc.users.create([NewUser(name="notg")], "web:admin")
    off = await make(svc, 1, base=3000)
    await svc.users.set_status(off, False, "web:admin")
    blocked = await make(svc, 1, base=4000)
    svc.ctx.db.call(repo.update_user, blocked[0], can_message=False)
    pv = await bs.preview(None)
    reasons = {r.user_id: r.reason for r in pv.recipients}
    assert reasons[ok[0]] is None
    assert reasons[nobot[0]] == "bot_not_started"
    assert reasons[res.user_ids[0]] == "no_tg_id"
    assert reasons[off[0]] == "not_active"
    assert reasons[blocked[0]] == "cannot_message"
    assert len(pv.included) == 1 and len(pv.excluded) == 4
    assert all(r.reason_text for r in pv.excluded)


async def test_send_all_report_and_link_content(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    ids = await make(svc, 2)
    await make(svc, 1, started=False, base=2000)
    report = await bs.start("Привет, {name}! {link} <b>x</b>", "bot:1")
    assert report.sent == 2 and report.skipped == 1 and report.pending == 0
    first = sender.sent[0]
    user = await svc.users.get(ids[0])
    assert user
    assert "Привет, u1000!" in first.text and "&lt;b&gt;x&lt;/b&gt;" in first.text
    assert first.button and first.button.url == svc.users.link(user)
    assert svc.users.link(user).startswith("https://t.me/webproxy?")
    skipped = [i for i in report.items if i.result.startswith("skipped:")]
    assert skipped[0].result_text == "пропущен: бот не запущен"
    again = await bs.report(report.broadcast_id)
    assert again.sent == 2


async def test_tg_link_placeholder(svc: Svc, bs: BroadcastService, sender: FakeSender) -> None:
    ids = await make(svc, 1)
    await bs.start("{tg_link}", "bot:1")
    user = await svc.users.get(ids[0])
    assert user
    import html

    assert html.unescape(sender.sent[0].text) == svc.users.tg_link(user)


async def test_rate_limit_at_most_20_per_second(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    await make(svc, 65)
    await bs.start("hi {name}", "bot:1")
    times = [s.at for s in sender.sent]
    assert len(times) == 65
    for i, t in enumerate(times):
        in_window = [x for x in times[i:] if x < t + 1.0 - 1e-6]
        assert len(in_window) <= 20
    assert times[-1] >= 3.19  # 64 gaps of 0.05 s


async def test_retry_after_is_waited_and_retried(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    await make(svc, 2)
    sender.script[1000] = [retry_after(7, 1000)]
    report = await bs.start("hi", "bot:1")
    assert report.sent == 2
    assert sender.to(1000)[0].at >= 7.0


async def test_forbidden_clears_can_message(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    ids = await make(svc, 2)
    sender.script[1000] = [forbidden(1000)]
    report = await bs.start("hi", "bot:1")
    assert report.forbidden == 1 and report.sent == 1
    extra = svc.ctx.db.call(repo.get_user_extra, ids[0])
    assert extra and not extra.can_message
    nxt = await bs.preview(None)
    assert nxt.recipients[0].reason == "cannot_message"


async def test_other_errors_recorded_and_do_not_stop(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    await make(svc, 3)
    sender.script[1001] = [RuntimeError("secret detail")]
    report = await bs.start("hi", "bot:1")
    assert report.errors == 1 and report.sent == 2
    stored = [i.result for i in report.items]
    assert "error:RuntimeError" in stored
    assert "secret detail" not in repr(svc.ctx.db.call(repo.list_broadcast_items, 1))


async def test_cancel_and_resume_sends_each_once(
    svc: Svc, bs: BroadcastService, sender: FakeSender
) -> None:
    await make(svc, 5)
    bid = await bs.create("hi", "bot:1")

    async def stop_after_two(done: int, total: int) -> None:
        if done == 2:
            bs.cancel(bid)

    part = await bs.run(bid, on_progress=stop_after_two)
    assert part.sent == 2 and part.pending == 3
    final = await bs.run(bid)
    assert final.sent == 5 and final.pending == 0
    assert sorted(s.chat_id for s in sender.sent) == [1000, 1001, 1002, 1003, 1004]
    await bs.run(bid)
    assert len(sender.sent) == 5


async def test_invalid_input(svc: Svc, bs: BroadcastService) -> None:
    await make(svc, 1)
    with pytest.raises(OperationRejected):
        await bs.create("   ", "bot:1")
    with pytest.raises(OperationRejected):
        await bs.create("x" * 5000, "bot:1")
    with pytest.raises(OperationRejected):
        await bs.create("hi", "bot:1", user_ids=[])
    with pytest.raises(OperationRejected):
        await bs.report(99)


def test_render_template_escapes_everything() -> None:
    out = render_template(
        "<i>{name}</i> {unknown}", {"name": "<script>", "link": "", "tg_link": "", "expires": ""}
    )
    assert out == "&lt;i&gt;&lt;script&gt;&lt;/i&gt; {unknown}"


async def test_rate_limiter_standalone() -> None:
    from tests.bot.helpers import FakeTime

    t = FakeTime()
    rl = RateLimiter(10, clock=t.monotonic, sleep=t.sleep)
    for _ in range(11):
        await rl.acquire()
    assert abs(t.now - 1.0) < 1e-9


# ------------------------------------------------------------------ notifier


async def test_messenger_gives_up_after_repeated_retry_after(svc: Svc, sender: FakeSender) -> None:
    sender.script[5] = [retry_after(1, 5) for _ in range(10)]
    m = (
        Messenger(svc.ctx.pipeline, svc.ctx.pipeline, sleep=sender.time.sleep)
        if False
        else Messenger(sender, svc.ctx.pipeline, sleep=sender.time.sleep, max_retries=2)
    )
    assert (await m.deliver(5, "x")).startswith("error:")
    assert sender.time.now == 3.0


async def test_on_failure_alerts_admins_without_secrets() -> None:
    notifier = FakeNotifier()
    hook = make_on_failure(notifier)
    secret = "ab" * 16
    await hook(
        ApplyFailure(
            run_id=7,
            reason="create",
            actor="web:admin",
            error=f"Не удалось применить изменения (health): secret={secret}",
            rolled_back=True,
            rollback_errors=(),
        )
    )
    text = notifier.messages[0]
    assert "#7" in text and "create" in text and "откатены" in text
    assert secret not in text


async def test_on_failure_never_raises_and_flags_dirty_rollback() -> None:
    class Broken:
        async def notify_admins(self, text: str) -> None:
            raise RuntimeError("down")

    hook = make_on_failure(Broken())
    await hook(ApplyFailure(1, "x", "system", "err", True, ("nft",)))
    rec = FakeNotifier()
    await make_on_failure(rec)(ApplyFailure(1, "x", "system", "err", True, ("nft",)))
    assert "не полностью" in rec.messages[0]


async def test_late_bound_notifier() -> None:
    late = LateBoundNotifier()
    await late.notify_admins("dropped")  # nobody bound yet
    target = FakeNotifier()
    late.bind(target)
    await late.notify_admins("seen")
    assert target.messages == ["seen"]


# ------------------------------------------------------------------ review fixes


def test_scrub_secrets_masks_bot_tokens() -> None:
    from tgpanel.services.notifier import scrub_secrets

    token = "123456789:" + "Ab_-" * 9
    out = scrub_secrets(f"POST /bot{token}/sendMessage failed")
    assert token not in out and "[redacted]" in out
    assert scrub_secrets("short 12345:abc stays") == "short 12345:abc stays"


def test_render_message_only_documented_placeholders() -> None:
    from tgpanel.services.notifier import render_message

    values = {"name": "<N>", "link": "L&", "tg_link": "T", "expires": "E", "days": "3"}
    assert render_message("{name} {link} {expires} {days} {x} {name.__class__}", values) == (
        "&lt;N&gt; L&amp; E 3 {x} {name.__class__}"
    )
    assert render_message("<b>{name}</b>", values, escape=False) == "<b><N></b>"


async def test_running_registry_is_cleaned(svc: Svc, bs: BroadcastService) -> None:
    await make(svc, 2)
    bid = await bs.create("hi", "bot:1")
    task = asyncio.create_task(bs.run(bid))
    await asyncio.sleep(0)
    await task
    assert bs._running == {} and not bs.is_running(bid)
    await bs.run(bid)
    assert bs._running == {}


async def test_broadcast_uses_shared_messenger_and_limiter(svc: Svc, sender: FakeSender) -> None:
    await svc.users.load_hostname()
    limiter = RateLimiter(20, clock=sender.time.monotonic, sleep=sender.time.sleep)
    messenger = Messenger(sender, svc.ctx.pipeline, limiter=limiter, sleep=sender.time.sleep)
    service = BroadcastService(svc.ctx.pipeline, svc.ctx.db, svc.users, sender, messenger=messenger)
    await make(svc, 30)
    # 10 other messages go through the same limiter first
    for _ in range(10):
        await messenger.deliver(1, "x")
    await service.start("hi", "bot:1")
    times = [s.at for s in sender.sent]
    assert len(times) == 40
    for i, t in enumerate(times):
        assert len([x for x in times[i:] if x < t + 1.0 - 1e-6]) <= 20
