"""display_name: free-form Unicode label next to the technical `name`."""

from __future__ import annotations

import pytest

from tests.services.conftest import Svc
from tgpanel.db import repo
from tgpanel.domain.models import UserRecord, shown_name
from tgpanel.services.api import NewUser
from tgpanel.services.errors import UserServiceError
from tgpanel.services.requests import RequestKind, RequestService, telegram_display_name

UNICODE_NAMES = ["Дмитрий Жабкин", "Инга Базанова 🐉", "山田 太郎", "  <b>x</b> & «y»  "]


async def one(svc: Svc, name: str = "user_1", **kw: object) -> int:
    res = await svc.users.create([NewUser(name=name, **kw)], "web:admin")  # type: ignore[arg-type]
    assert res.ok, res.error
    return res.user_ids[0]


@pytest.mark.parametrize("label", UNICODE_NAMES)
async def test_create_stores_unicode_display_name_and_keeps_name(svc: Svc, label: str) -> None:
    uid = await one(svc, "user_93455874", display_name=label)
    user = await svc.users.get(uid)
    assert user and user.name == "user_93455874" and user.display_name == label.strip()
    assert user.profile_name == f"u{uid}"
    assert svc.profile_names() == [f"u{uid}"]  # personal data never reaches the proxy files
    svc.assert_clean("")


async def test_display_name_validation_on_create(svc: Svc) -> None:
    for bad in ("я" * 101, "line1\nline2", "a\x00b", "tab\there"):
        res = await svc.users.create([NewUser(name="x", display_name=bad)], "web:admin")
        assert not res.ok and res.error
    assert svc.ctx.db.call(repo.all_users) == []


async def test_empty_display_name_is_allowed_and_not_unique(svc: Svc) -> None:
    a = await one(svc, "a")
    b = await one(svc, "b", display_name="Same")
    c = await one(svc, "c", display_name="Same")  # display names need not be unique
    labels = {u.id: u.display_name for u in svc.ctx.db.call(repo.all_users)}
    assert labels == {a: "", b: "Same", c: "Same"}


async def test_update_meta_display_name_is_db_only_and_audited(svc: Svc) -> None:
    uid = await one(svc, "user_5")
    svc.fake.clear_calls()
    runs = len(svc.runs())
    await svc.users.update_meta(uid, "web:admin", display_name="  Инга 🐉  ")
    user = await svc.users.get(uid)
    assert user and user.display_name == "Инга 🐉" and user.name == "user_5"
    assert svc.fake.calls_of("systemctl") == [] and len(svc.runs()) == runs
    audit = svc.audit_text()
    assert "user.update_meta" in audit and "display_name" in audit
    assert "Инга" not in audit  # values are not copied into the audit trail
    await svc.users.update_meta(uid, "web:admin", display_name="")  # clearing is allowed
    cleared = await svc.users.get(uid)
    assert cleared and cleared.display_name == ""


async def test_update_meta_display_name_independent_of_name(svc: Svc) -> None:
    uid = await one(svc, "alpha", display_name="Альфа")
    await svc.users.update_meta(uid, "x", name="beta")
    user = await svc.users.get(uid)
    assert user and (user.name, user.display_name) == ("beta", "Альфа")
    await svc.users.update_meta(uid, "x", display_name="Бета")
    user = await svc.users.get(uid)
    assert user and (user.name, user.display_name) == ("beta", "Бета")


async def test_update_meta_display_name_validation(svc: Svc) -> None:
    uid = await one(svc, "alpha", display_name="ok")
    for bad in ("я" * 101, "a\nb", "a\rb", "\x07"):
        with pytest.raises(UserServiceError):
            await svc.users.update_meta(uid, "x", display_name=bad)
    user = await svc.users.get(uid)
    assert user and user.display_name == "ok"
    await svc.users.update_meta(uid, "x", display_name="я" * 100)  # the limit itself is fine


def test_shown_name_falls_back_to_name() -> None:
    def rec(name: str, display: str) -> UserRecord:
        from tgpanel.domain.models import UserStatus

        return UserRecord(1, name, "a" * 32, UserStatus.ACTIVE, 1, "127.64.0.1", None, None,
                          display_name=display)  # fmt: skip

    assert shown_name(rec("user_1", "")) == "user_1"
    assert shown_name(rec("user_1", "Макс")) == "Макс"


def test_positional_construction_still_works_and_display_name_defaults_empty() -> None:
    from tgpanel.domain.models import UserStatus

    user = UserRecord(1, "n", "a" * 32, UserStatus.ACTIVE, 1, "127.64.0.1", None, None)
    assert user.display_name == ""


def test_telegram_display_name_helper() -> None:
    assert telegram_display_name("  Анна\n  Смирнова ") == "Анна Смирнова"
    assert telegram_display_name("Саша 🚀") == "Саша 🚀"
    assert telegram_display_name("", "nick") == "@nick"
    assert telegram_display_name("   ") == ""
    assert telegram_display_name("Ж" * 300) == "Ж" * 100
    assert telegram_display_name("a\x00b" + chr(0x2028) + "c") == "abc"


async def test_requested_user_gets_telegram_full_name_as_display_name(svc: Svc) -> None:
    await svc.users.load_hostname()
    rs = RequestService(svc.ctx.pipeline, svc.ctx.db, svc.users)
    out = await rs.submit(777, "bob", "Инга Базанова 🐉")
    assert out.kind is RequestKind.CREATED and out.request
    done = await rs.approve(out.request.id, None, "bot:1")
    assert done.kind is RequestKind.ISSUED and done.user
    assert done.user.display_name == "Инга Базанова 🐉"
    assert done.user.name == "Инга Базанова 🐉"  # technical name: the existing logic
