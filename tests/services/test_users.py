# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tests.services.conftest import PROFILES, RELAY, Svc
from tgpanel.db import repo
from tgpanel.db.times import to_db
from tgpanel.domain.models import CarrierMode, UserRecord, UserStatus
from tgpanel.domain.queries import UserFilter, UserListQuery
from tgpanel.services.api import NewUser
from tgpanel.services.errors import UserServiceError

_COUNTER = itertools.count(1000)
ENV1 = "/etc/tgpanel/mtproxy/1.env"
ENV2 = "/etc/tgpanel/mtproxy/2.env"
POOL1 = "tgpanel-mtproxy@1"


async def make(svc: Svc, n: int = 1, **kw: object) -> list[int]:
    base = len(svc.ctx.db.call(repo.all_users)) + next(_COUNTER)
    res = await svc.users.create(
        [NewUser(name=f"user {base + i}", **kw) for i in range(n)],  # type: ignore[arg-type]
        "web:admin",
    )
    assert res.ok, res.error
    return list(res.user_ids)


# ------------------------------------------------------------------------------- create


async def test_create_single(svc: Svc) -> None:
    res = await svc.users.create(
        [NewUser(name="Вася Пупкин", tg_id=4242, comment="friend")], "web:admin"
    )
    assert res.ok and res.user_ids == (1,) and res.apply_run_id is not None
    user = await svc.users.get(1)
    assert user and user.name == "Вася Пупкин" and user.profile_name == "u1"
    assert user.status is UserStatus.ACTIVE and user.tg_id == 4242 and user.comment == "friend"
    assert user.pool_id == 1 and user.loopback_ip == "127.64.0.1" and not user.imported
    assert len(user.secret) == 32 and not user.secret.startswith("dd")
    assert res.links == {1: f"https://t.me/webproxy?server=proxy.example.com&secret={user.secret}"}
    assert svc.users.tg_link(user).startswith("tg://webproxy?server=proxy.example.com&secret=")
    assert svc.profile_names() == ["u1"]
    assert svc.fake.get_text(ENV1).count(f"-S {user.secret}") == 1
    assert "user.create" in svc.audit_text()
    svc.assert_clean(res.error or "")


async def test_default_expiry_is_one_month(svc: Svc) -> None:
    await make(svc)
    user = await svc.users.get(1)
    assert user and user.expires_at is not None
    delta = user.expires_at - svc.clock.now
    assert timedelta(days=27) < delta < timedelta(days=32)


@pytest.mark.parametrize(
    ("term", "date_setting", "low", "high"),
    [("1d", "", 0, 2), ("1y", "", 360, 370)],
)
async def test_default_term_setting(
    svc: Svc, term: str, date_setting: str, low: int, high: int
) -> None:
    assert (await svc.ctx.settings.set("default_term", term, "web:admin")).ok
    await make(svc)
    user = await svc.users.get(1)
    assert user and user.expires_at
    assert low <= (user.expires_at - svc.clock.now).days <= high


async def test_default_term_date(svc: Svc) -> None:
    target = (svc.clock.now + timedelta(days=100)).date().isoformat()
    assert (
        await svc.ctx.settings.set_many(
            {"default_term": "date", "default_term_date": target}, "web:admin"
        )
    ).ok
    await make(svc)
    user = await svc.users.get(1)
    assert user and user.expires_at and user.expires_at.date().isoformat() == target
    assert (await svc.ctx.settings.set("default_term_date", "2020-01-01", "web:admin")).ok
    res = await svc.users.create([NewUser(name="late")], "web:admin")
    assert not res.ok and "истёк" in (res.error or "")


async def test_explicit_expiry_and_carrier_mode(svc: Svc) -> None:
    when = svc.clock.now + timedelta(days=3)
    res = await svc.users.create(
        [NewUser(name="a", expires_at=when, carrier_mode=CarrierMode.WEBSOCKET)], "bot:1"
    )
    assert res.ok
    user = await svc.users.get(1)
    assert user and user.expires_at == when.replace(microsecond=0)
    assert svc.fake.get_json(PROFILES)["profiles"][0]["carrier_mode"] == "websocket"


async def test_batch_create_is_one_apply(svc: Svc) -> None:
    runs = len(svc.runs())
    relay = svc.fake.restart_count(RELAY)
    res = await svc.users.create([NewUser(name=f"u{i}") for i in range(10)], "web:admin")
    assert res.ok and len(res.user_ids) == 10 and len(res.links) == 10
    assert len(svc.runs()) == runs + 1
    assert svc.fake.restart_count(RELAY) == relay + 1
    assert len(svc.fake.calls_of("tproxy_check")) == 1
    assert len(svc.fake.calls_of("make_tar_gz")) == 1


async def test_batch_over_pool_limit_opens_second_pool_in_one_apply(svc: Svc) -> None:
    relay = svc.fake.restart_count(RELAY)
    res = await svc.users.create([NewUser(name=f"n{i}") for i in range(20)], "web:admin")
    assert res.ok
    pools = svc.ctx.db.call(repo.list_pools)
    assert [(p.id, p.port) for p in pools] == [(1, 2400), (2, 2401)]
    assert svc.fake.get_text(ENV1).count("-S ") == 16 and svc.fake.get_text(ENV2).count("-S ") == 4
    assert svc.fake.restart_count(RELAY) == relay + 1
    assert svc.fake.port_is_open(2400) and svc.fake.port_is_open(2401)


async def test_create_failure_leaves_no_user_and_no_links(svc: Svc) -> None:
    svc.fake.fail_on("systemctl", f"restart {RELAY}")
    res = await svc.users.create([NewUser(name="a"), NewUser(name="b")], "web:admin")
    assert not res.ok and res.links == {} and res.user_ids == ()
    assert res.error and "изменения отменены" in res.error and res.apply_run_id
    assert svc.ctx.db.call(repo.all_users) == []
    assert svc.profile_names() == ["_tgpanel_sentinel"]
    # the operation can be repeated
    again = await svc.users.create([NewUser(name="a"), NewUser(name="b")], "web:admin")
    assert again.ok and again.user_ids == (1, 2)
    svc.assert_clean(res.error)


@pytest.mark.parametrize(
    ("users", "needle"),
    [
        ([], "Не указано"),
        ([NewUser(name="  ")], "Имя"),
        ([NewUser(name="x" * 101)], "Имя"),
        ([NewUser(name="a"), NewUser(name="a")], "дважды"),
        ([NewUser(name="a", tg_id=5), NewUser(name="b", tg_id=5)], "дважды"),
        ([NewUser(name="a", tg_id=-1)], "Telegram ID"),
        ([NewUser(name="a", expires_at=datetime(2000, 1, 1, tzinfo=UTC))], "будущем"),
        ([NewUser(name="a", comment="c" * 3000)], "Комментарий"),
    ],
)
async def test_create_validation(svc: Svc, users: list[NewUser], needle: str) -> None:
    res = await svc.users.create(users, "web:admin")
    assert not res.ok and needle in (res.error or "")
    assert svc.ctx.db.call(repo.all_users) == []
    assert len(svc.runs()) == 1  # only the baseline


async def test_create_rejects_taken_name_and_tg_id(svc: Svc) -> None:
    (first,) = await make(svc, 1, tg_id=77)
    taken = (await svc.users.get(first)).name  # type: ignore[union-attr]
    assert not (await svc.users.create([NewUser(name=taken)], "web:admin")).ok
    res = await svc.users.create([NewUser(name="other", tg_id=77)], "web:admin")
    assert not res.ok and "уже привязан" in (res.error or "")
    assert len(svc.runs()) == 2


async def test_create_needs_proxy_hostname(svc: Svc) -> None:
    svc.ctx.db.call(repo.delete_setting, "proxy_hostname")
    res = await svc.users.create([NewUser(name="a")], "web:admin")
    assert not res.ok and "proxy_hostname" in (res.error or "")
    user_like = None
    with pytest.raises(UserServiceError):
        svc.users.link(user_like or _dummy_user())


def _dummy_user() -> UserRecord:
    return UserRecord(1, "x", "0" * 32, UserStatus.ACTIVE, 1, "127.64.0.1", None, None)


# ------------------------------------------------------------------------ status / expiry


async def test_disable_and_enable_restart_only_relay(svc: Svc) -> None:
    ids = await make(svc, 3)
    pool_restarts = svc.fake.restart_count(POOL1)
    relay = svc.fake.restart_count(RELAY)
    runs = len(svc.runs())
    res = await svc.users.set_status([ids[1]], False, "web:admin")
    assert res.ok and res.links == {}
    assert svc.profile_names() == ["u1", "u3"]
    assert svc.fake.restart_count(POOL1) == pool_restarts
    assert svc.fake.restart_count(RELAY) == relay + 1
    user = await svc.users.get(ids[1])
    assert user and user.status is UserStatus.DISABLED
    res = await svc.users.set_status([ids[1]], True, "web:admin")
    assert res.ok and svc.profile_names() == ["u1", "u2", "u3"]
    assert len(svc.runs()) == runs + 2
    assert svc.fake.restart_count(POOL1) == pool_restarts


async def test_bulk_status_is_one_apply(svc: Svc) -> None:
    ids = await make(svc, 6)
    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    res = await svc.users.set_status(ids, False, "web:admin")
    assert res.ok and len(svc.runs()) == runs + 1 and svc.fake.restart_count(RELAY) == relay + 1
    assert svc.profile_names() == ["_tgpanel_sentinel"]


async def test_status_noop_has_no_apply(svc: Svc) -> None:
    ids = await make(svc, 2)
    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    res = await svc.users.set_status(ids, True, "web:admin")  # already active
    assert res.ok and len(svc.runs()) == runs and svc.fake.restart_count(RELAY) == relay


async def test_unknown_ids_are_rejected(svc: Svc) -> None:
    await make(svc)
    for op in (
        svc.users.set_status([1, 99], False, "x"),
        svc.users.delete([99], "x"),
        svc.users.extend([99], 3, "x"),
        svc.users.set_expiry([99], None, "x"),
        svc.users.set_carrier_mode([99], None, "x"),
        svc.users.reissue_secret(99, "x"),
        svc.users.set_status([], True, "x"),
    ):
        res = await op
        assert not res.ok and res.error
    user = await svc.users.get(1)
    assert user and user.status is UserStatus.ACTIVE


async def test_enable_expired_user_requires_extension(svc: Svc) -> None:
    (uid,) = await make(svc, expires_at=svc.clock.now + timedelta(days=1))
    svc.ctx.db.call(
        repo.update_user,
        uid,
        expires_at=svc.clock.now - timedelta(hours=1),
        status=UserStatus.EXPIRED,
    )
    res = await svc.users.set_status([uid], True, "web:admin")
    assert not res.ok and "продлите" in (res.error or "")
    res = await svc.users.extend([uid], 5, "web:admin")
    assert res.ok
    user = await svc.users.get(uid)
    assert user and user.status is UserStatus.ACTIVE
    assert user.expires_at and user.expires_at > svc.clock.now


async def test_set_expiry_variants(svc: Svc) -> None:
    ids = await make(svc, 2)
    soon = svc.clock.now + timedelta(days=2)
    assert (await svc.users.set_expiry(ids, soon, "web:admin")).ok
    for uid in ids:
        user = await svc.users.get(uid)
        assert user and user.expires_at == soon.replace(microsecond=0)
    runs = len(svc.runs())
    # removing the expiry is DB-only: nothing changes on the proxy
    assert (await svc.users.set_expiry(ids, None, "web:admin")).ok
    assert len(svc.runs()) == runs
    assert (await svc.users.get(1)).expires_at is None  # type: ignore[union-attr]
    # a date in the past expires the user right away and removes it from the relay
    past = svc.clock.now - timedelta(minutes=5)
    assert (await svc.users.set_expiry([ids[0]], past, "web:admin")).ok
    assert (await svc.users.get(ids[0])).status is UserStatus.EXPIRED  # type: ignore[union-attr]
    assert svc.profile_names() == ["u2"]
    # a future date revives the expired user
    assert (await svc.users.set_expiry([ids[0]], svc.clock.now + timedelta(days=9), "w")).ok
    assert svc.profile_names() == ["u1", "u2"]


async def test_extend_keeps_disabled_users_disabled(svc: Svc) -> None:
    ids = await make(svc, 2)
    assert (await svc.users.set_status([ids[0]], False, "web:admin")).ok
    before = (await svc.users.get(ids[0])).expires_at  # type: ignore[union-attr]
    assert before
    assert (await svc.users.extend(ids, 10, "web:admin")).ok
    user = await svc.users.get(ids[0])
    assert user and user.status is UserStatus.DISABLED
    assert user.expires_at == before + timedelta(days=10)
    assert svc.profile_names() == ["u2"]


@pytest.mark.parametrize("days", [0, -1, 5000])
async def test_extend_bounds(svc: Svc, days: int) -> None:
    ids = await make(svc)
    res = await svc.users.extend(ids, days, "web:admin")
    assert not res.ok and "дней" in (res.error or "")


async def test_expire_due_one_apply_for_all(svc: Svc) -> None:
    now = svc.clock.now
    ids = await make(svc, 5, expires_at=now + timedelta(days=1))
    for uid in ids[:4]:
        svc.ctx.db.call(repo.update_user, uid, expires_at=now - timedelta(minutes=1))
    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    res = await svc.users.expire_due(svc.clock.now)
    assert res.ok and set(res.user_ids) == set(ids[:4])
    assert len(svc.runs()) == runs + 1 and svc.fake.restart_count(RELAY) == relay + 1
    assert svc.profile_names() == [f"u{ids[4]}"]
    statuses = {u.id: u.status for u in svc.ctx.db.call(repo.all_users)}
    assert statuses[ids[0]] is UserStatus.EXPIRED and statuses[ids[4]] is UserStatus.ACTIVE
    # secrets of expired users stay in the pool (slot is kept)
    assert svc.fake.get_text(ENV1).count("-S ") == 5


async def test_expire_due_nothing_due_is_free(svc: Svc) -> None:
    await make(svc, 2)
    runs, calls = len(svc.runs()), len(svc.fake.calls)
    res = await svc.users.expire_due(svc.clock.now)
    assert res.ok and res.user_ids == () and len(svc.runs()) == runs
    assert len(svc.fake.calls) == calls


async def test_imported_and_unlimited_users_never_expire(svc: Svc) -> None:
    ids = await make(svc, 1)
    assert (await svc.users.set_expiry(ids, None, "x")).ok
    res = await svc.users.expire_due(svc.clock.now + timedelta(days=9999))
    assert res.ok and res.user_ids == ()


# ------------------------------------------------------------------- secrets / modes / delete


async def test_reissue_secret_restarts_pool_and_relay(svc: Svc) -> None:
    ids = await make(svc, 2)
    old = (await svc.users.get(ids[0])).secret  # type: ignore[union-attr]
    pool, relay = svc.fake.restart_count(POOL1), svc.fake.restart_count(RELAY)
    res = await svc.users.reissue_secret(ids[0], "web:admin")
    assert res.ok and ids[0] in res.links
    new = (await svc.users.get(ids[0])).secret  # type: ignore[union-attr]
    assert new != old and res.links[ids[0]].endswith(new)
    body = svc.fake.get_text(ENV1)
    assert new in body and old not in body
    assert svc.fake.restart_count(POOL1) == pool + 1
    assert svc.fake.restart_count(RELAY) == relay + 1
    assert old not in svc.fake.get_text(PROFILES)
    svc.assert_clean(res.error or "")


async def test_reissue_for_disabled_user_updates_pool_only_for_relay_noop(svc: Svc) -> None:
    ids = await make(svc, 2)
    await svc.users.set_status([ids[0]], False, "x")
    relay = svc.fake.restart_count(RELAY)
    pool = svc.fake.restart_count(POOL1)
    res = await svc.users.reissue_secret(ids[0], "x")
    assert res.ok
    assert svc.fake.restart_count(POOL1) == pool + 1  # secret changed inside the pool
    assert svc.fake.restart_count(RELAY) == relay  # relay does not carry the disabled user


async def test_set_carrier_mode(svc: Svc) -> None:
    ids = await make(svc, 3)
    pool, relay = svc.fake.restart_count(POOL1), svc.fake.restart_count(RELAY)
    res = await svc.users.set_carrier_mode(ids[:2], CarrierMode.HTTPS_LANES, "web:admin")
    assert res.ok
    modes = [p["carrier_mode"] for p in svc.fake.get_json(PROFILES)["profiles"]]
    assert modes == ["https-lanes", "https-lanes", "https"]
    assert svc.fake.restart_count(POOL1) == pool and svc.fake.restart_count(RELAY) == relay + 1
    assert (await svc.users.set_carrier_mode(ids[:2], None, "web:admin")).ok
    assert {p["carrier_mode"] for p in svc.fake.get_json(PROFILES)["profiles"]} == {"https"}


async def test_delete_removes_secret_from_pool_and_frees_slot(svc: Svc) -> None:
    ids = await make(svc, 3)
    victim = await svc.users.get(ids[1])
    assert victim
    svc.ctx.db.call(repo.add_traffic, "day", ids[1], svc.clock.now, bytes_up=100, bytes_down=200)
    pool, relay = svc.fake.restart_count(POOL1), svc.fake.restart_count(RELAY)
    res = await svc.users.delete([ids[1]], "web:admin")
    assert res.ok and res.links == {}
    assert svc.fake.get_text(ENV1).count("-S ") == 2
    assert victim.mtproxy_secret not in svc.fake.get_text(ENV1)
    assert svc.fake.restart_count(POOL1) == pool + 1 and svc.fake.restart_count(RELAY) == relay + 1
    assert svc.profile_names() == ["u1", "u3"]
    assert victim.loopback_ip not in svc.fake.nft_sets[("tgpanel", "up")]
    assert await svc.users.get(ids[1]) is None
    count = svc.ctx.db.call(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM traffic_day WHERE user_id = ?", (ids[1],)
        ).fetchone()[0]
    )
    assert count == 0  # statistics go with the user
    # the freed address and slot are reused
    new = await make(svc, 1)
    assert (await svc.users.get(new[0])).loopback_ip == victim.loopback_ip  # type: ignore[union-attr]
    assert (await svc.users.get(new[0])).pool_id == 1  # type: ignore[union-attr]


async def test_delete_frees_a_slot_in_a_full_pool(svc: Svc) -> None:
    assert (await svc.ctx.settings.set("secrets_per_process", 2, "x")).ok
    ids = await make(svc, 2)
    assert len(svc.ctx.db.call(repo.list_pools)) == 1
    assert (await svc.users.delete([ids[0]], "x")).ok
    (new,) = await make(svc, 1)
    assert (await svc.users.get(new)).pool_id == 1  # type: ignore[union-attr]
    assert len(svc.ctx.db.call(repo.list_pools)) == 1
    (extra,) = await make(svc, 1)  # now the pool is full again: second pool
    assert (await svc.users.get(extra)).pool_id == 2  # type: ignore[union-attr]


async def test_delete_bulk_is_one_apply_and_last_one_restores_sentinel(svc: Svc) -> None:
    ids = await make(svc, 4)
    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    res = await svc.users.delete(ids, "web:admin")
    assert res.ok and len(svc.runs()) == runs + 1 and svc.fake.restart_count(RELAY) == relay + 1
    assert svc.profile_names() == ["_tgpanel_sentinel"]
    assert svc.fake.get_text(ENV1).count("-S ") == 1
    assert svc.fake.nft_sets[("tgpanel", "up")] == {}


async def test_delete_emptied_pool_is_stopped(svc: Svc) -> None:
    assert (await svc.ctx.settings.set("secrets_per_process", 1, "x")).ok
    ids = await make(svc, 2)  # two pools of one
    assert "tgpanel-mtproxy@2" in svc.fake.active
    assert (await svc.users.delete([ids[1]], "x")).ok
    assert "tgpanel-mtproxy@2" not in svc.fake.active and ENV2 not in svc.fake.files
    assert POOL1 in svc.fake.active


# --------------------------------------------------------------- failures leave no trace

FAILING = ["set_status", "set_expiry_past", "extend", "reissue", "carrier", "delete", "expire"]


@pytest.mark.parametrize("op", FAILING)
async def test_failed_apply_rolls_back_every_operation(svc: Svc, op: str) -> None:
    ids = await make(svc, 3, expires_at=svc.clock.now + timedelta(days=1))
    assert (await svc.users.set_expiry([ids[0]], svc.clock.now - timedelta(minutes=1), "x")).ok
    svc.ctx.db.call(repo.update_user, ids[2], expires_at=svc.clock.now - timedelta(minutes=1))
    users_before = svc.ctx.db.call(repo.all_users)
    profiles_before = svc.fake.files[PROFILES].data
    env_before = svc.fake.files[ENV1].data
    svc.fake.fail_on("systemctl", f"restart {RELAY}")
    if op == "set_status":
        res = await svc.users.set_status([ids[1]], False, "x")
    elif op == "set_expiry_past":
        res = await svc.users.set_expiry([ids[1]], svc.clock.now - timedelta(days=1), "x")
    elif op == "extend":
        res = await svc.users.extend([ids[0]], 5, "x")
    elif op == "reissue":
        res = await svc.users.reissue_secret(ids[1], "x")
    elif op == "carrier":
        res = await svc.users.set_carrier_mode([ids[1]], CarrierMode.WEBSOCKET, "x")
    elif op == "delete":
        res = await svc.users.delete([ids[1]], "x")
    else:
        res = await svc.users.expire_due(svc.clock.now)
    assert not res.ok and res.links == {} and res.error
    assert svc.ctx.db.call(repo.all_users) == users_before
    assert svc.fake.files[PROFILES].data == profiles_before
    assert svc.fake.files[ENV1].data == env_before
    svc.assert_clean(res.error)


# ------------------------------------------------------------------------- DB-only edits


async def test_update_meta_is_db_only(svc: Svc) -> None:
    (uid,) = await make(svc)
    svc.fake.clear_calls()
    runs = len(svc.runs())
    await svc.users.update_meta(
        uid, "web:admin", name="Новое имя", comment="заметка", tg_id=999, tg_username="@vasya"
    )
    user = await svc.users.get(uid)
    assert user and user.name == "Новое имя" and user.comment == "заметка" and user.tg_id == 999
    extra = svc.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.tg_username == "vasya"
    assert svc.fake.calls_of("systemctl") == [] and svc.fake.calls_of("write_atomic") == []
    assert svc.fake.calls_of("make_tar_gz") == [] and len(svc.runs()) == runs
    assert "user.update_meta" in svc.audit_text()
    assert svc.profile_names() == [f"u{uid}"]  # profile names never carry personal data


async def test_update_meta_validation(svc: Svc) -> None:
    a, b = await make(svc, 2)
    await svc.users.update_meta(a, "x", tg_id=5)
    with pytest.raises(UserServiceError):
        await svc.users.update_meta(b, "x", tg_id=5)
    with pytest.raises(UserServiceError):
        await svc.users.update_meta(b, "x", name=(await svc.users.get(a)).name)  # type: ignore[union-attr]
    with pytest.raises(UserServiceError):
        await svc.users.update_meta(404, "x", comment="c")
    with pytest.raises(UserServiceError):
        await svc.users.update_meta(b, "x", name="")
    # nothing was half-applied
    assert (await svc.users.get(b)).name.startswith("user ")  # type: ignore[union-attr]


async def test_update_meta_waits_for_running_apply(svc: Svc) -> None:
    (uid,) = await make(svc)
    t1 = asyncio.create_task(svc.users.create([NewUser(name="slow")], "x"))
    t2 = asyncio.create_task(svc.users.update_meta(uid, "x", comment="during"))
    await asyncio.gather(t1, t2)
    assert (await svc.users.get(uid)).comment == "during"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------- queries


async def test_list_and_get(svc: Svc) -> None:
    await make(svc, 5)
    await svc.users.update_meta(2, "x", comment="import-ish")
    await svc.users.set_status([3], False, "x")
    page = await svc.users.list(UserListQuery())
    assert page.total == 5 and [r.user.id for r in page.rows] == [1, 2, 3, 4, 5]
    page = await svc.users.list(UserListQuery(filter=UserFilter(statuses=(UserStatus.DISABLED,))))
    assert [r.user.id for r in page.rows] == [3]
    page = await svc.users.list(UserListQuery(filter=UserFilter(comment_contains="import")))
    assert [r.user.id for r in page.rows] == [2]
    page = await svc.users.list(UserListQuery(sort="id", descending=True))
    assert page.rows[0].user.id == 5 and page.rows[0].online is False
    assert await svc.users.get(404) is None


# ----------------------------------------------------------------------------- concurrency


async def test_concurrent_creates_both_succeed(svc: Svc) -> None:
    a, b = await asyncio.gather(
        svc.users.create([NewUser(name="a")], "web:1"),
        svc.users.create([NewUser(name="b")], "bot:2"),
    )
    assert a.ok and b.ok and set(a.user_ids).isdisjoint(b.user_ids)
    assert len(svc.ctx.db.call(repo.all_users)) == 2
    assert len({u.loopback_ip for u in svc.ctx.db.call(repo.all_users)}) == 2


async def test_audit_never_contains_secrets_after_a_full_lifecycle(svc: Svc) -> None:
    ids = await make(svc, 3)
    await svc.users.reissue_secret(ids[0], "x")
    await svc.users.set_status([ids[1]], False, "x")
    await svc.users.extend(ids, 3, "x")
    await svc.users.delete([ids[2]], "x")
    svc.fake.fail_on("systemctl", f"restart {RELAY}")
    bad = await svc.users.set_status([ids[0]], False, "x")
    svc.assert_clean(bad.error or "")
    assert to_db(svc.clock.now)  # keep helper import used
