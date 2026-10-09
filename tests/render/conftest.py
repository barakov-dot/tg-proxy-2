from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tgpanel.domain.models import (
    CarrierMode,
    DesiredState,
    PoolRecord,
    UserRecord,
    UserStatus,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "upstream"
GOLDEN = Path(__file__).resolve().parent / "golden"
SENTINEL = "f" * 32


def fixture_bytes(variant: str, name: str) -> bytes:
    return (FIXTURES / variant / name).read_bytes()


def fixture_text(variant: str, name: str) -> str:
    return (FIXTURES / variant / name).read_text()


def secret(n: int) -> str:
    return f"{n:032x}"


def make_user(
    uid: int,
    *,
    pool_id: int = 1,
    status: UserStatus = UserStatus.ACTIVE,
    sec: str | None = None,
    mode: CarrierMode | None = None,
) -> UserRecord:
    return UserRecord(
        id=uid,
        name=f"name{uid}",
        secret=sec if sec is not None else secret(uid),
        status=status,
        pool_id=pool_id,
        loopback_ip=f"127.64.0.{uid}",
        carrier_mode=mode,
        expires_at=None,
    )


def make_state(
    users: tuple[UserRecord, ...], pools: tuple[PoolRecord, ...] | None = None, **kw: object
) -> DesiredState:
    if pools is None:
        pools = (PoolRecord(1, 2400, 8900), PoolRecord(2, 2401, 8901))
    return DesiredState(users=users, pools=pools, sentinel_secret=SENTINEL, **kw)  # type: ignore[arg-type]


@pytest.fixture
def golden() -> Callable[[str, bytes | str], None]:
    def check(name: str, actual: bytes | str) -> None:
        data = actual.encode() if isinstance(actual, str) else actual
        path = GOLDEN / name
        if os.environ.get("UPDATE_GOLDEN") == "1" or not path.exists():
            path.write_bytes(data)
        assert path.read_bytes() == data, f"golden mismatch: {name}"

    return check
