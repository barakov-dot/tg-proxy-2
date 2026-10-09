"""Loopback address allocator in 127.64.0.0/16 (PLAN 3.1)."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

NETWORK = ipaddress.IPv4Network("127.64.0.0/16")
_PREFIX = "127.64."


class AddressExhaustedError(RuntimeError):
    """No free loopback address left."""


def is_valid_loopback_ip(value: str) -> bool:
    """True if ``value`` is 127.64.<hi>.<lo> with lo not in {0, 255}."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    if str(ip) != value or ip not in NETWORK:
        return False
    return ip.packed[3] not in (0, 255)


def _candidates() -> Iterable[str]:
    for hi in range(256):
        for lo in range(1, 255):
            yield f"{_PREFIX}{hi}.{lo}"


def capacity() -> int:
    return 256 * 254


QUARANTINE_KEY = "addr.quarantine"  # settings key: JSON {ip: iso timestamp of release}
QUARANTINE_TTL = timedelta(hours=24)


def parse_quarantine(raw: str | None, now: datetime) -> dict[str, datetime]:
    """Quarantined addresses still within the TTL; malformed input yields what is valid."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, datetime] = {}
    if not isinstance(data, dict):
        return out
    for ip, ts in data.items():
        try:
            released = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if released.tzinfo is None:
            released = released.replace(tzinfo=UTC)
        if is_valid_loopback_ip(str(ip)) and now - released < QUARANTINE_TTL:
            out[str(ip)] = released
    return out


def quarantine_released(raw: str | None, released_ips: Iterable[str], now: datetime) -> str:
    """New JSON value for the settings key after ``released_ips`` were freed at ``now``."""
    current = parse_quarantine(raw, now)
    for ip in released_ips:
        current[ip] = now
    return dump_quarantine(current)


def dump_quarantine(entries: Mapping[str, datetime]) -> str:
    return json.dumps({ip: ts.astimezone(UTC).isoformat() for ip, ts in sorted(entries.items())})


def allocate_addresses(
    used: Iterable[str], count: int = 1, quarantined: Iterable[str] = ()
) -> list[str]:
    """Return ``count`` lowest free addresses (unique, ascending by (hi, lo)).

    ``quarantined`` addresses (recently released, see ``parse_quarantine``) are skipped so a
    new user never reuses the address of a user deleted moments ago.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    taken = set(used) | set(quarantined)
    out: list[str] = []
    if count == 0:
        return out
    for addr in _candidates():
        if addr in taken:
            continue
        out.append(addr)
        if len(out) == count:
            return out
    raise AddressExhaustedError("no free loopback addresses in 127.64.0.0/16")
