"""Pure helpers shared by the real and fake SystemOps: validation, scrubbing, tar handling."""

from __future__ import annotations

import gzip
import io
import ipaddress
import posixpath
import re
import tarfile

from tgpanel.system.ops import SystemOpsError

UNIT_RE = re.compile(r"^[a-zA-Z0-9@._-]+$")
PROP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
NFT_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SECRET_SCRUB_RE = re.compile(r"(?:dd)?[0-9a-fA-F]{32}")

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
MAX_TAR_TOTAL_BYTES = 256 * 1024 * 1024


def scrub(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Redact anything that looks like a proxy secret and truncate to `limit` bytes."""
    cleaned = SECRET_SCRUB_RE.sub("[redacted]", text)
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


def build_tar_gz(members: dict[str, bytes]) -> bytes:
    """Deterministic tar.gz: sorted names, zero mtime/uid/gid, mode 0600 members."""
    if sum(len(v) for v in members.values()) > MAX_TAR_TOTAL_BYTES:
        raise SystemOpsError("archive too large")
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in sorted(members):
            validate_member_name(name)
            data = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def parse_tar_gz(source: str | bytes) -> dict[str, bytes]:
    """Read regular files only; reject traversal, links, devices, dirs, oversize archives."""
    result: dict[str, bytes] = {}
    try:
        tar_cm = (
            tarfile.open(name=source, mode="r:gz")
            if isinstance(source, str)
            else tarfile.open(fileobj=io.BytesIO(source), mode="r:gz")
        )
        with tar_cm as tar:
            total = 0
            for member in tar:
                if not member.isreg():
                    raise SystemOpsError("archive contains a non-regular member")
                validate_member_name(member.name)
                if member.name in result:
                    raise SystemOpsError("archive contains duplicate members")
                total += member.size
                if total > MAX_TAR_TOTAL_BYTES:
                    raise SystemOpsError("archive too large")
                fobj = tar.extractfile(member)
                if fobj is None:
                    raise SystemOpsError("archive member unreadable")
                result[member.name] = fobj.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemOpsError(f"cannot read archive: {type(exc).__name__}") from None
    return result
