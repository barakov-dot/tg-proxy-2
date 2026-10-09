"""Loopback address allocator in 127.64.0.0/16 (PLAN 3.1)."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

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


def allocate_addresses(used: Iterable[str], count: int = 1) -> list[str]:
    """Return ``count`` lowest free addresses (unique, ascending by (hi, lo))."""
    if count < 0:
        raise ValueError("count must be >= 0")
    taken = set(used)
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
