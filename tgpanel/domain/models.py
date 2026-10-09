"""Frozen domain models shared by all layers. CONTRACT: change only via the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"


class CarrierMode(StrEnum):
    HTTPS = "https"
    HTTPS_LANES = "https-lanes"
    WEBSOCKET = "websocket"
    WEBSOCKET_LANES = "websocket-lanes"


@dataclass(frozen=True, slots=True)
class PoolRecord:
    id: int
    port: int  # client-facing MTProxy port (-H), 2400..2463
    stats_port: int  # MTProxy stats port (-p), 8900..8963
    managed: bool = True


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: int
    name: str
    secret: str  # as stored: 32 hex, or "dd"+32 hex for imported users
    status: UserStatus
    pool_id: int
    loopback_ip: str  # 127.64.x.y
    carrier_mode: CarrierMode | None  # None = global default
    expires_at: datetime | None  # UTC
    comment: str = ""
    tg_id: int | None = None
    imported: bool = False
    source_profile_name: str | None = None

    @property
    def profile_name(self) -> str:
        return f"u{self.id}"

    @property
    def mtproxy_secret(self) -> str:
        """Base secret passed to MTProxy (no 'dd' prefix)."""
        s = self.secret
        return s[2:] if len(s) == 34 and s.startswith("dd") else s


@dataclass(frozen=True, slots=True)
class RelayLimits:
    max_sessions_global: int = 1024
    max_streams_global: int = 16384


@dataclass(frozen=True, slots=True)
class DesiredState:
    """Everything render/ needs to produce all proxy-side files. Pure data."""

    users: tuple[UserRecord, ...]
    pools: tuple[PoolRecord, ...]
    sentinel_secret: str  # used only when no user is ACTIVE
    default_carrier_mode: CarrierMode = CarrierMode.HTTPS
    relay_limits: RelayLimits = field(default_factory=RelayLimits)
    secrets_per_process: int = 16
    mtp_workers: int = 1
    mtp_max_connections: int = 4096
    panel_hostname: str = ""
