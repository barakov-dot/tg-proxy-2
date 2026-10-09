"""Secret generation, validation and parsing (PLAN 3.1, 3.10, 13.4)."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_DD_HEX32 = re.compile(r"dd[0-9a-fA-F]{32}")


def generate_secret() -> str:
    """New user secret: 32 hex, no 'dd' prefix."""
    return secrets.token_hex(16)


def is_valid_secret(value: str) -> bool:
    """32 hex or 'dd' + 32 hex."""
    return bool(_HEX32.fullmatch(value) or _DD_HEX32.fullmatch(value))


def base_secret(value: str) -> str:
    """Secret as passed to MTProxy: lowercase, 'dd' prefix removed."""
    if not is_valid_secret(value):
        raise ValueError("invalid secret format")
    v = value.lower()
    return v[2:] if len(v) == 34 else v


@dataclass(frozen=True, slots=True)
class ParsedSecret:
    raw: str  # kept exactly as imported (link depends on it)
    base: str  # lowercase 32 hex for MTProxy
    has_dd_prefix: bool


def parse_imported_secret(value: str) -> ParsedSecret:
    value = value.strip()
    if not is_valid_secret(value):
        raise ValueError("invalid secret format")
    return ParsedSecret(raw=value, base=base_secret(value), has_dd_prefix=len(value) == 34)
