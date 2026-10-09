"""SystemOps: the ONLY gateway to the operating system. CONTRACT: change only via orchestrator.

Real implementation runs commands as argument lists (never a shell). Fake implementation
(tests) keeps an in-memory filesystem and records every call; it can inject failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FileStat:
    mode: int  # e.g. 0o640
    owner: str
    group: str
    size: int


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    output: str  # never contains secrets (implementation must scrub)


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int  # 0 = connection failed
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class SetCounter:
    bytes: int
    packets: int


@dataclass(frozen=True, slots=True)
class CertInfo:
    issuer: str
    not_before: str  # ISO-8601 UTC
    not_after: str  # ISO-8601 UTC
    valid_chain: bool


class SystemOpsError(Exception):
    """Any failed system operation. Message must not contain secrets."""


class SystemOps(Protocol):
    # --- files (all paths absolute) ---
    async def read_file(self, path: str) -> bytes: ...
    async def exists(self, path: str) -> bool: ...
    async def stat(self, path: str) -> FileStat: ...
    async def list_dir(self, path: str) -> list[str]: ...
    async def write_atomic(
        self, path: str, data: bytes, *, mode: int, owner: str, group: str
    ) -> None:
        """Write to temp file in the same dir, fsync, set owner/mode, rename over `path`."""
        ...

    async def remove(self, path: str) -> None: ...
    async def make_tar_gz(self, dest: str, members: dict[str, bytes]) -> None:
        """Create 0600 archive: archive-name -> content."""
        ...

    async def read_tar_gz(self, path: str) -> dict[str, bytes]: ...

    # --- systemd ---
    async def systemctl(self, action: str, unit: str) -> None:
        """action in {start,stop,restart,enable,disable,enable-now,disable-now,mask-now,unmask,
        daemon-reload}. Raises SystemOpsError on non-zero exit."""
        ...

    async def is_active(self, unit: str) -> bool: ...
    async def unit_property(self, unit: str, prop: str) -> str: ...

    # --- network / health ---
    async def wait_tcp_open(self, host: str, port: int, timeout_s: float) -> bool: ...
    async def http_get(self, url: str, timeout_s: float) -> HttpResult: ...
    async def resolve(self, hostname: str) -> tuple[list[str], list[str]]:
        """Returns (A records, AAAA records)."""
        ...

    async def public_ipv4(self) -> str | None: ...
    async def tls_cert_info(self, hostname: str) -> CertInfo | None: ...

    # --- proxy tooling ---
    async def tproxy_check(self, config_path: str, profiles_path: str) -> CheckResult:
        """`tproxy-server -config .. -profiles-file .. -check`."""
        ...

    async def caddy_validate(self, caddyfile_path: str, env: dict[str, str]) -> CheckResult: ...

    # --- nftables ---
    async def nft_load_file(self, path: str) -> None: ...
    async def nft_list_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        """ip -> counter. Parsed from `nft -j list set inet <table> <set>`."""
        ...

    async def nft_add_elements(self, table: str, set_name: str, ips: list[str]) -> None: ...
    async def nft_delete_elements(self, table: str, set_name: str, ips: list[str]) -> None: ...

    # --- locking ---
    async def acquire_lock(self, path: str, timeout_s: float) -> LockHandle: ...


class LockHandle(Protocol):
    async def release(self) -> None: ...
