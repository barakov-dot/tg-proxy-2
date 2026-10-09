"""Formatting helpers (HTML escaping of every user-supplied string goes through ``esc``)."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from tgpanel.bot import icons, texts


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def fmt_dt(value: datetime | None, tz: str, empty: str = "—") -> str:
    if value is None:
        return empty
    return value.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")


def status_mark(status: str) -> str:
    return {"active": icons.ACTIVE, "disabled": icons.DISABLED, "expired": icons.EXPIRED}.get(
        status, "?"
    )


def status_ru(status: str) -> str:
    return texts.STATUS_RU.get(status, status)
