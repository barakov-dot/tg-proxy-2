from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.services.conftest import Svc
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.domain.models import UserStatus
from tgpanel.services.requests import RequestKind, RequestService


@pytest.fixture
async def rs(svc: Svc) -> RequestService:
    await svc.users.load_hostname()
    return RequestService(svc.ctx.pipeline, svc.ctx.db, svc.users)


def set_setting(svc: Svc, key: str, value: str) -> None:
    svc.ctx.db.call(repo.set_setting, key, value)


async def test_single_pending_per_tg_id(svc: Svc, rs: RequestService) -> None:
    a = await rs.submit(10, "bob", "Bob")
    b = await rs.submit(10, "bob", "Bob")
    assert a.kind is RequestKind.CREATED and b.kind is RequestKind.ALREADY_PENDING
    assert len(await rs.list_pending()) == 1


async def test_concurrent_submits_create_one_request(svc: Svc, rs: RequestService) -> None:
    outs = await asyncio.gather(*(rs.submit(10, None, "Bob") for _ in range(5)))
    assert sum(o.kind is RequestKind.CREATED for o in outs) == 1
    assert len(await rs.list_pending()) == 1


async def test_blacklist(svc: Svc, rs: RequestService) -> None:
    set_setting(svc, "bot_blacklist", "5; 10,abc 11")
    assert (await rs.submit(10, None, "x")).kind is RequestKind.BLACKLISTED
    assert (await rs.submit(12, None, "x")).kind is RequestKind.CREATED
    assert await rs.list_pending() != []


async def test_rate_limit_window(svc: Svc, rs: RequestService) -> None:
    for _ in range(3):
        out = await rs.submit(10, None, "x")
        assert out.request
        await rs.reject(out.request.id, "bot:1")
    assert (await rs.submit(10, None, "x")).kind is RequestKind.RATE_LIMITED
    svc.clock.now += timedelta(hours=2)
    assert (await rs.submit(10, None, "x")).kind is RequestKind.CREATED


async def test_approve_one_apply_link_after_success_and_idempotent(
    svc: Svc, rs: RequestService
) -> None:
    out = await rs.submit(10, "bob", "Bob")
    assert out.request
    runs = len(svc.runs())
    ok = await rs.approve(out.request.id, Term.MONTH, "bot:1")
    assert ok.kind is RequestKind.ISSUED and ok.link and ok.user
    assert ok.link == svc.users.link(ok.user)
    assert ok.user.expires_at is not None and ok.user.tg_id == 10
    assert len(svc.runs()) == runs + 1
    extra = svc.ctx.db.call(repo.get_user_extra, ok.user.id)
    assert extra and extra.bot_started and extra.can_message and extra.tg_username == "bob"
    again = await rs.approve(out.request.id, Term.MONTH, "bot:1")
    assert again.kind is RequestKind.ALREADY_DECIDED and again.link is None
    assert len(svc.runs()) == runs + 1


async def test_concurrent_approvals_create_one_user(svc: Svc, rs: RequestService) -> None:
    out = await rs.submit(10, None, "Bob")
    assert out.request
    res = await asyncio.gather(*(rs.approve(out.request.id, None, "bot:1") for _ in range(4)))
    assert sum(r.kind is RequestKind.ISSUED for r in res) == 1
    assert len(svc.ctx.db.call(repo.all_users)) == 1


async def test_reject_and_unknown(svc: Svc, rs: RequestService) -> None:
    out = await rs.submit(10, None, "Bob")
    assert out.request
    assert (await rs.reject(out.request.id, "bot:1")).kind is RequestKind.REJECTED
    assert (await rs.reject(out.request.id, "bot:1")).kind is RequestKind.ALREADY_DECIDED
    assert (await rs.approve(out.request.id, None, "bot:1")).kind is RequestKind.ALREADY_DECIDED
    assert (await rs.approve(999, None, "bot:1")).kind is RequestKind.NOT_FOUND
    assert svc.ctx.db.call(repo.all_users) == []


async def test_open_mode_issues_immediately(svc: Svc, rs: RequestService) -> None:
    set_setting(svc, "issuance_mode", "open")
    order: list[str] = []

    async def preparing() -> None:
        order.append("preparing")

    out = await rs.submit(10, "bob", "Bob", on_preparing=preparing)
    assert out.kind is RequestKind.ISSUED and out.link and order == ["preparing"]
    assert (await rs.submit(10, "bob", "Bob")).kind is RequestKind.HAS_ACCESS


async def test_failed_apply_no_link_request_stays_pending_and_retry(
    svc: Svc, rs: RequestService
) -> None:
    set_setting(svc, "issuance_mode", "open")
    pipeline = svc.ctx.pipeline
    real = pipeline._execute

    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    pipeline._execute = boom  # type: ignore[method-assign]
    out = await rs.submit(10, None, "Bob")
    assert out.kind is RequestKind.FAILED and out.link is None and out.error
    assert svc.ctx.db.call(repo.all_users) == []
    pipeline._execute = real  # type: ignore[method-assign]
    retry = await rs.submit(10, None, "Bob")  # pressing the button again retries
    assert retry.kind is RequestKind.ISSUED and retry.link


async def test_name_clash_gets_suffix(svc: Svc, rs: RequestService) -> None:
    for tg in (10, 11):
        out = await rs.submit(tg, None, "Same Name")
        assert out.request
        res = await rs.approve(out.request.id, None, "bot:1")
        assert res.kind is RequestKind.ISSUED
    names = sorted(u.name for u in svc.ctx.db.call(repo.all_users))
    assert names == ["Same Name", "Same Name (11)"]


async def test_multiline_name_is_flattened(svc: Svc, rs: RequestService) -> None:
    out = await rs.submit(10, None, "A\nB  C")
    assert out.request
    res = await rs.approve(out.request.id, None, "bot:1")
    assert res.user and res.user.name == "A B C"


async def test_register_start_binds_existing_user(svc: Svc, rs: RequestService) -> None:
    from tgpanel.services.api import NewUser

    res = await svc.users.create([NewUser(name="Old", tg_id=10)], "web:admin")
    uid = res.user_ids[0]
    info = await rs.register_start(10, "oldie")
    assert info.user and info.first_start
    info2 = await rs.register_start(10, "oldie")
    assert not info2.first_start
    extra = svc.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.bot_started and extra.tg_username == "oldie"
    assert (await rs.register_start(99, None)).user is None
    # blocked user starting again can be messaged again
    svc.ctx.db.call(repo.update_user, uid, can_message=False)
    await rs.register_start(10, "oldie")
    extra = svc.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.can_message
    assert (await svc.users.get(uid)).status is UserStatus.ACTIVE  # type: ignore[union-attr]


# ------------------------------------------------------------------ review fixes


def test_parse_ids_ascii_only() -> None:
    from tgpanel.services.requests import parse_ids

    assert parse_ids("1, 22;333  ٤٤ ² x5 " + "7" * 40) == {1, 22, 333}


async def test_blacklist_edit_and_garbage(svc: Svc, rs: RequestService) -> None:
    set_setting(svc, "bot_blacklist", "١٢٣ 5")
    assert await rs.blacklist_ids() == [5]
    assert await rs.blacklist_edit(6, True, "bot:1") == [5, 6]
    assert await rs.blacklist_edit(5, False, "bot:1") == [6]
    from tgpanel.apply.errors import OperationRejected

    with pytest.raises(OperationRejected):
        await rs.blacklist_edit(0, True, "bot:1")
    assert (await rs.submit(6, None, "x")).kind is RequestKind.BLACKLISTED
    assert "blacklist.add" in svc.audit_text()


async def test_open_mode_global_hourly_limit_falls_back_to_approval(
    svc: Svc, rs: RequestService
) -> None:
    set_setting(svc, "issuance_mode", "open")
    set_setting(svc, "open_mode_max_per_hour", "2")
    kinds = [(await rs.submit(tg, None, f"n{tg}")).kind for tg in (10, 11, 12)]
    assert kinds == [RequestKind.ISSUED, RequestKind.ISSUED, RequestKind.CREATED]
    assert len(await rs.list_pending()) == 1
    svc.clock.now += timedelta(hours=2)  # the window has passed
    assert (await rs.submit(13, None, "n13")).kind is RequestKind.ISSUED


async def test_open_mode_limit_counts_concurrent_requests(svc: Svc, rs: RequestService) -> None:
    set_setting(svc, "issuance_mode", "open")
    set_setting(svc, "open_mode_max_per_hour", "3")
    outs = await asyncio.gather(*(rs.submit(100 + i, None, f"c{i}") for i in range(8)))
    assert sum(o.kind is RequestKind.ISSUED for o in outs) == 3
    assert len(svc.ctx.db.call(repo.all_users)) == 3


async def test_concurrent_open_requests_share_applies(svc: Svc, rs: RequestService) -> None:
    set_setting(svc, "issuance_mode", "open")
    set_setting(svc, "open_mode_max_per_hour", "50")
    before = len(svc.runs())
    outs = await asyncio.gather(*(rs.submit(200 + i, None, f"c{i}") for i in range(6)))
    assert all(o.kind is RequestKind.ISSUED and o.link for o in outs)
    # the pipeline coalesces operations queued together: far fewer applies than requests
    assert len(svc.runs()) - before < 6


async def test_locks_are_released(svc: Svc, rs: RequestService) -> None:
    out = await rs.submit(10, None, "Bob")
    assert out.request
    await asyncio.gather(*(rs.approve(out.request.id, None, "bot:1") for _ in range(3)))
    await rs.reject(out.request.id, "bot:1")
    assert rs._locks == {}
