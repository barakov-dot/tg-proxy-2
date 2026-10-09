"""Pure helpers shared by the real and fake SystemOps: validation, scrubbing, tar handling."""

from __future__ import annotations

import contextlib
import gzip
import io
import ipaddress
import os
import posixpath
import re
import tarfile
from collections.abc import Collection, Iterator, Mapping
from typing import IO

from tgpanel.system.ops import LocalFile, SystemOpsError

UNIT_RE = re.compile(r"^[a-zA-Z0-9@._-]+$")
PROP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
NFT_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SECRET_SCRUB_RE = re.compile(r"(?:dd)?[0-9a-fA-F]{32}")
# base64url-looking tokens (>= 22 chars, with a digit or an upper-case letter), not parts of paths
B64_SCRUB_RE = re.compile(
    r"(?<![A-Za-z0-9_/.@-])(?=[A-Za-z0-9_-]*[0-9A-Z])[A-Za-z0-9_-]{22,}(?![A-Za-z0-9_/.@-])"
)

SYSTEMCTL_ACTIONS: dict[str, tuple[str, ...]] = {
    "start": ("start",),
    "stop": ("stop",),
    "restart": ("restart",),
    "enable": ("enable",),
    "disable": ("disable",),
    "enable-now": ("enable", "--now"),
    "disable-now": ("disable", "--now"),
    "mask-now": ("mask", "--now"),
    "unmask": ("unmask",),
    "daemon-reload": ("daemon-reload",),
}

MAX_OUTPUT_BYTES = 4096
MAX_TAR_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # safety cap, enforced while streaming


def scrub(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Redact anything that looks like a proxy secret and truncate to `limit` bytes."""
    cleaned = B64_SCRUB_RE.sub("[redacted]", SECRET_SCRUB_RE.sub("[redacted]", text))
    raw = cleaned.encode("utf-8", "replace")
    if len(raw) > limit:
        return raw[:limit].decode("utf-8", "ignore")
    return cleaned


def validate_unit(unit: str) -> str:
    if not UNIT_RE.fullmatch(unit) or unit.startswith("-") or len(unit) > 256:
        raise SystemOpsError("invalid unit name")
    return unit


def validate_systemctl(action: str, unit: str) -> tuple[str, ...]:
    """Return systemctl argv tail (without the binary) for a whitelisted action."""
    if action not in SYSTEMCTL_ACTIONS:
        raise SystemOpsError(f"systemctl action not allowed: {scrub(action, 64)}")
    if action == "daemon-reload":
        return SYSTEMCTL_ACTIONS[action]
    validate_unit(unit)
    return (*SYSTEMCTL_ACTIONS[action], unit)


def validate_property(prop: str) -> str:
    if not PROP_RE.fullmatch(prop):
        raise SystemOpsError("invalid unit property name")
    return prop


def validate_nft_ident(name: str, what: str) -> str:
    if not NFT_IDENT_RE.fullmatch(name):
        raise SystemOpsError(f"invalid nft {what} name")
    return name


def validate_path(path: str) -> str:
    """Absolute, normalised path without NUL bytes."""
    if (
        not path.startswith("/")
        or "\x00" in path
        or "//" in path
        or posixpath.normpath(path) != path
    ):
        raise SystemOpsError("path must be absolute and normalised")
    return path


def validate_ipv4(ip: str) -> str:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        raise SystemOpsError("invalid IPv4 address") from None
    return str(addr)


def validate_env(env: dict[str, str]) -> dict[str, str]:
    for k, v in env.items():
        if not ENV_NAME_RE.fullmatch(k) or "\x00" in v:
            raise SystemOpsError("invalid environment variable")
    return env


def normalize_unit(unit: str) -> str:
    """`foo.service` and `foo` are the same unit for state keeping."""
    return unit.removesuffix(".service")


# --- tar ---------------------------------------------------------------------------------


def validate_member_name(name: str) -> str:
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\x00" in name
        or "\\" in name
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise SystemOpsError("unsafe archive member name")
    return name


class _Budget:
    """Running total of archive payload bytes, checked against the (patchable) module cap."""

    def __init__(self) -> None:
        self.total = 0

    def add(self, n: int) -> None:
        self.total += n
        if self.total > MAX_TAR_TOTAL_BYTES:
            raise SystemOpsError("archive too large")


class _CappedReader(io.RawIOBase):
    """Wraps a decompressing stream; fails when more than the cap was decompressed."""

    def __init__(self, raw: gzip.GzipFile) -> None:
        self._raw = raw
        self._budget = _Budget()

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self._raw.read(len(b))
        self._budget.add(len(data))
        b[: len(data)] = data
        return len(data)


def write_tar_gz(fileobj: IO[bytes], members: Mapping[str, bytes | LocalFile]) -> None:
    """Write a deterministic tar.gz to ``fileobj``; ``LocalFile`` members are streamed."""
    budget = _Budget()
    with (
        gzip.GzipFile(fileobj=fileobj, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in sorted(members):
            validate_member_name(name)
            item = members[name]
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if isinstance(item, LocalFile):
                validate_path(item.path)
                info.size = os.path.getsize(item.path)
                budget.add(info.size)
                with open(item.path, "rb") as src:
                    tar.addfile(info, src)  # tarfile copies in chunks: no full read
            else:
                info.size = len(item)
                budget.add(info.size)
                tar.addfile(info, io.BytesIO(item))


def build_tar_gz(members: Mapping[str, bytes | LocalFile]) -> bytes:
    """Deterministic tar.gz as bytes (tests and small archives)."""
    buf = io.BytesIO()
    write_tar_gz(buf, members)
    return buf.getvalue()


@contextlib.contextmanager
def _open_tar(source: str | bytes) -> Iterator[tarfile.TarFile]:
    raw: IO[bytes] = open(source, "rb") if isinstance(source, str) else io.BytesIO(source)
    try:
        gz = gzip.GzipFile(fileobj=raw, mode="rb")
        with tarfile.open(fileobj=_CappedReader(gz), mode="r|") as tar:
            yield tar
    finally:
        raw.close()


def iter_tar_members(
    source: str | bytes, names: Collection[str] | None, sink: dict[str, bytes] | None = None
) -> dict[str, bytes]:
    """Stream the archive once; keep the data of ``names`` (all if None). Validates every header."""
    wanted = None if names is None else set(names)
    result: dict[str, bytes] = {} if sink is None else sink
    seen: set[str] = set()
    budget = _Budget()
    try:
        with _open_tar(source) as tar:
            for member in tar:
                if not member.isreg():
                    raise SystemOpsError("archive contains a non-regular member")
                validate_member_name(member.name)
                if member.name in seen:
                    raise SystemOpsError("archive contains duplicate members")
                seen.add(member.name)
                budget.add(member.size)
                if wanted is not None and member.name not in wanted:
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    raise SystemOpsError("archive member unreadable")
                result[member.name] = fobj.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemOpsError(f"cannot read archive: {type(exc).__name__}") from None
    return result


def extract_tar_member_to_file(source: str | bytes, name: str, dest: str) -> bool:
    """Stream one member into the local file ``dest`` (created 0600). False if absent."""
    validate_path(dest)
    budget = _Budget()
    found = False
    try:
        with _open_tar(source) as tar:
            for member in tar:
                if not member.isreg():
                    raise SystemOpsError("archive contains a non-regular member")
                validate_member_name(member.name)
                budget.add(member.size)
                if member.name != name:
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    raise SystemOpsError("archive member unreadable")
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    while chunk := fobj.read(1024 * 1024):
                        out.write(chunk)
                found = True
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemOpsError(f"cannot read archive: {type(exc).__name__}") from None
    return found


def parse_tar_gz(source: str | bytes) -> dict[str, bytes]:
    """Read regular files only; reject traversal, links, devices, dirs, oversize archives."""
    return iter_tar_members(source, None)
