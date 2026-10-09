"""Strict parsing of request values (ASCII digits only, bounded length)."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime

DIGITS_RE = re.compile(r"[0-9]+")
DECIMAL_RE = re.compile(r"[0-9]{1,9}(?:[.,][0-9]{1,6})?")
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
MAX_ID = 2**62  # path ids and database integers stay inside SQLite's 64-bit range
MIN_DATE = 1970
MAX_DATE = 2100


def parse_uint(raw: str, *, max_digits: int = 18) -> int | None:
    """Non-negative integer from ASCII digits (no '²', no Arabic-Indic digits, no sign)."""
    value = raw.strip()
    if len(value) > max_digits or DIGITS_RE.fullmatch(value) is None:
        return None
    return int(value)


def parse_decimal(raw: str) -> float | None:
    value = raw.strip()
    if DECIMAL_RE.fullmatch(value) is None:
        return None
    return float(value.replace(",", "."))


def parse_day(raw: str) -> datetime | None:
    """``YYYY-MM-DD`` (ASCII) within 1970..2100, else None."""
    value = raw.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return day if MIN_DATE <= day.year <= MAX_DATE else None


def valid_username(raw: str) -> bool:
    return raw == "" or USERNAME_RE.fullmatch(raw) is not None


def ip_key(raw: str) -> str | None:
    """Limiter key of an address: IPv6 -> its /64, IPv4-mapped IPv6 -> the IPv4 address."""
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)
