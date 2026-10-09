# ruff: noqa: RUF001
"""Typed registry of panel settings stored in the ``settings`` table (values are strings)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from tgpanel.apply.errors import SettingsError
from tgpanel.db import repo
from tgpanel.domain.expiry import Term
from tgpanel.domain.models import CarrierMode

# internal (not user-editable) keys
KEY_SENTINEL_SECRET = "sentinel_secret"  # noqa: S105 - setting name, not a secret
KEY_PROFILES_HASH = "apply.profiles_hash"
KEY_MTPROXY_FACTS = "mtproxy_facts"

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _int_range(low: int, high: int, label: str) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw.strip())
        except ValueError:
            raise SettingsError(f"{label}: нужно целое число") from None
        if not low <= value <= high:
            raise SettingsError(f"{label}: допустимо от {low} до {high}")
        return value

    return parse


def _choice(options: tuple[str, ...], label: str) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        value = raw.strip()
        if value not in options:
            raise SettingsError(f"{label}: допустимо {', '.join(options)}")
        return value

    return parse


def _hostname(raw: str) -> str:
    value = raw.strip().lower()
    if value and not _HOST_RE.match(value):
        raise SettingsError("Некорректное имя хоста")
    return value


def _timezone(raw: str) -> str:
    value = raw.strip()
    try:
        ZoneInfo(value)
    except Exception:
        raise SettingsError("Неизвестный часовой пояс") from None
    return value


def _iso_date(raw: str) -> str:
    value = raw.strip()
    if value:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise SettingsError("Дата должна быть в формате ГГГГ-ММ-ДД") from None
    return value


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    default: str
    parse: Callable[[str], Any]
    affects_proxy: bool
    title: str


def _spec(
    key: str, default: str, parse: Callable[[str], Any], affects_proxy: bool, title: str
) -> tuple[str, SettingSpec]:
    return key, SettingSpec(key, default, parse, affects_proxy, title)


SPECS: dict[str, SettingSpec] = dict(
    [
        _spec(
            "secrets_per_process",
            "16",
            _int_range(1, 16, "Секретов на процесс MTProxy"),
            True,
            "Секретов на процесс MTProxy",
        ),
        _spec(
            "default_term",
            Term.MONTH.value,
            _choice(tuple(t.value for t in Term), "Срок по умолчанию"),
            False,
            "Срок по умолчанию",
        ),
        _spec("default_term_date", "", _iso_date, False, "Дата для срока «конкретная дата»"),
        _spec(
            "carrier_mode_default",
            CarrierMode.HTTPS.value,
            _choice(tuple(m.value for m in CarrierMode), "carrier_mode"),
            True,
            "carrier_mode по умолчанию",
        ),
        _spec(
            "max_sessions_global",
            "1024",
            _int_range(16, 200_000, "max_sessions_global"),
            True,
            "Глобальный лимит сессий relay",
        ),
        _spec(
            "max_streams_global",
            "16384",
            _int_range(64, 2_000_000, "max_streams_global"),
            True,
            "Глобальный лимит потоков relay",
        ),
        _spec(
            "mtp_max_connections",
            "4096",
            _int_range(64, 1_000_000, "Соединений на процесс MTProxy"),
            True,
            "Соединений на процесс MTProxy (-C)",
        ),
        _spec("mtp_workers", "1", _int_range(1, 64, "Воркеров MTProxy"), True, "Воркеры MTProxy"),
        _spec("timezone", "UTC", _timezone, False, "Часовой пояс"),
        _spec(
            "backup_keep_last",
            "50",
            _int_range(1, 10_000, "Хранить последних бэкапов"),
            False,
            "Хранить последних бэкапов",
        ),
        _spec(
            "backup_keep_days",
            "30",
            _int_range(0, 3650, "Хранить по одному бэкапу в день, дней"),
            False,
            "Дней хранения ежедневных бэкапов",
        ),
        _spec("proxy_hostname", "", _hostname, False, "Имя хоста прокси"),
        _spec("panel_hostname", "", _hostname, False, "Имя хоста панели"),
        _spec(
            "issuance_mode",
            "approval",
            _choice(("approval", "open"), "Режим выдачи"),
            False,
            "Режим выдачи доступа",
        ),
    ]
)


def normalize(key: str, raw: object) -> str:
    """Validate ``raw`` for ``key`` and return the canonical stored string."""
    spec = SPECS.get(key)
    if spec is None:
        raise SettingsError("Неизвестная настройка")
    text = str(raw)
    value = spec.parse(text)
    return value if isinstance(value, str) else str(value)


@dataclass(frozen=True, slots=True)
class AppSettings:
    secrets_per_process: int
    default_term: str
    default_term_date: str
    carrier_mode_default: CarrierMode
    max_sessions_global: int
    max_streams_global: int
    mtp_max_connections: int
    mtp_workers: int
    timezone: str
    backup_keep_last: int
    backup_keep_days: int
    proxy_hostname: str
    panel_hostname: str
    issuance_mode: str


def read_settings(conn: sqlite3.Connection) -> AppSettings:
    stored = repo.all_settings(conn)
    values: dict[str, Any] = {}
    for key, spec in SPECS.items():
        raw = stored.get(key, spec.default)
        try:
            values[key] = spec.parse(raw)
        except SettingsError as exc:
            raise SettingsError(f"Сохранено некорректное значение настройки {key}: {exc}") from None
    values["carrier_mode_default"] = CarrierMode(values["carrier_mode_default"])
    return AppSettings(**values)
