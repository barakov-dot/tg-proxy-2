"""Collector settings read from the ``settings`` table with safe defaults.

These keys are not (yet) in ``apply/settings_spec.py``; unknown/invalid values fall back to the
defaults so a bad value can never stop the collector.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

KEY_POLL_INTERVAL = "poll_interval_s"
KEY_ACTIVITY_BYTES = "activity_min_bytes"
KEY_ACTIVITY_PACKETS = "activity_min_packets"
KEY_RETENTION_MINUTE = "retention_minute_days"
KEY_RETENTION_HOUR = "retention_hour_days"
KEY_ROLLUP_LAST = "collector.rollup_last_date"  # internal

DEFAULT_POLL_INTERVAL_S = 30
DEFAULT_RETENTION_MINUTE_DAYS = 14
DEFAULT_RETENTION_HOUR_DAYS = 180

NFT_TABLE = "tgpanel"

_HEX_RE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")


def int_setting(settings: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    raw = settings.get(key)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if low <= value <= high else default


def scrub(text: object, limit: int = 200) -> str:
    """Make an exception text safe for logs: no secret-looking hex strings, bounded length."""
    return _HEX_RE.sub("<redacted>", str(text))[:limit]


def log_warning(logger: logging.Logger, message: str, exc: BaseException) -> None:
    logger.warning("%s: %s: %s", message, type(exc).__name__, scrub(exc))
