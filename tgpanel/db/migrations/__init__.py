"""Numbered migrations: (version, SQL script). Append only; never edit applied ones."""

from __future__ import annotations

from tgpanel.db.migrations.m0001_initial import SQL as _M1
from tgpanel.db.migrations.m0002_display_name import SQL as _M2

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _M1), (2, _M2))
