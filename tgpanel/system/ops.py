"""SystemOps: the ONLY gateway to the operating system. CONTRACT: change only via orchestrator.

Real implementation runs commands as argument lists (never a shell). Fake implementation
(tests) keeps an in-memory filesystem and records every call; it can inject failures.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FileStat:
    mode: int  # e.g. 0o640
    owner: str
    group: str
    size: int


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total: int  # bytes
    used: int  # bytes


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


@dataclass(frozen=True, slots=True)
class LocalFile:
    """A local file whose content is streamed into an archive (never loaded into memory)."""

    path: str


class SystemOpsError(Exception):
    """Any failed system operation. Message must not contain secrets."""


class NftTableMissing(SystemOpsError):
    """The nft table or set does not exist (as opposed to any other nft failure)."""


class SystemOps(Protocol):
    # --- files (all paths absolute) ---
    async def read_file(self, path: str) -> bytes: ...
    async def exists(self, path: str) -> bool: ...
    async def stat(self, path: str) -> FileStat: ...
    async def disk_usage(self, path: str) -> DiskUsage:
        """Total and used bytes of the filesystem that holds ``path`` (``statvfs``)."""
        ...

    async def list_dir(self, path: str) -> list[str]: ...
    async def write_atomic(
        self, path: str, data: bytes, *, mode: int, owner: str, group: str
    ) -> None:
        """Write to temp file in the same dir, fsync, set owner/mode, rename over `path`."""
        ...

    async def remove(self, path: str) -> None: ...
    async def make_tar_gz(self, dest: str, members: Mapping[str, bytes | LocalFile]) -> None:
        """Create a 0600 archive atomically: archive-name -> content (``LocalFile`` is streamed)."""
        ...

    async def read_tar_gz(self, path: str) -> dict[str, bytes]:
        """All members in memory (small archives only; size is capped while streaming)."""
        ...

    async def read_tar_members(
        self, path: str, names: Collection[str] | None = None
    ) -> dict[str, bytes]:
        """Stream the archive and return only the named regular members (all if None)."""
        ...

    async def extract_tar_member(self, path: str, name: str, dest: str) -> bool:
        """Stream one member into the local file ``dest`` (0600). False if it is absent."""
        ...

    async def ensure_dir(self, path: str, mode: int, owner: str, group: str) -> None:
        """Create the directory (and parents) if needed and set mode/owner/group on it."""
        ...

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
    async def nft_check_file(self, path: str) -> CheckResult:
        """`nft -c -f <path>`: syntax/semantic check without applying (output is scrubbed)."""
        ...

    async def nft_list_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        """ip -> counter. Parsed from `nft -j list set inet <table> <set>`.

        Raises ``NftTableMissing`` when the table/set does not exist, any other
        ``SystemOpsError`` for other failures."""
        ...

    async def nft_delete_table(self, table: str) -> None:
        """`nft delete table inet <table>`; a missing table is not an error."""
        ...

    async def nft_add_elements(self, table: str, set_name: str, ips: list[str]) -> None: ...
    async def nft_delete_elements(self, table: str, set_name: str, ips: list[str]) -> None: ...

    # --- locking ---
    async def acquire_lock(self, path: str, timeout_s: float) -> LockHandle: ...


class LockHandle(Protocol):
    async def release(self) -> None: ...
