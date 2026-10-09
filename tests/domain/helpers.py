"""Synthetic builders for domain tests."""

from __future__ import annotations

from datetime import UTC, datetime

from tgpanel.domain.models import (
    CarrierMode,
    DesiredState,
    PoolRecord,
    RelayLimits,
    UserRecord,
    UserStatus,
)

NOW = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)


def secret_for(i: int) -> str:
    return f"{i:032x}"


def ip_for(i: int) -> str:
    return f"127.64.{i // 254}.{i % 254 + 1}"


def make_pool(n: int) -> PoolRecord:
    """Pool with id n+1 at index n."""
    return PoolRecord(id=n + 1, port=2400 + n, stats_port=8900 + n)


def make_user(
    i: int,
    pool_id: int = 1,
    status: UserStatus = UserStatus.ACTIVE,
    *,
    secret: str | None = None,
    ip: str | None = None,
    name: str | None = None,
    expires_at: datetime | None = None,
    carrier_mode: CarrierMode | None = None,
) -> UserRecord:
    return UserRecord(
        id=i,
        name=name or f"user{i}",
        secret=secret or secret_for(i),
        status=status,
        pool_id=pool_id,
        loopback_ip=ip or ip_for(i),
        carrier_mode=carrier_mode,
        expires_at=expires_at,
    )


def make_state(
    users: list[UserRecord],
    pools: list[PoolRecord] | None = None,
    **kwargs: object,
) -> DesiredState:
    # single-pool test states must satisfy: pools * mtp_max_connections >= max_streams_global
    kwargs.setdefault("relay_limits", RelayLimits(1024, 4096))
    return DesiredState(
        users=tuple(users),
        pools=tuple(pools if pools is not None else [make_pool(0)]),
        sentinel_secret=secret_for(999_999),
        **kwargs,  # type: ignore[arg-type]
    )
