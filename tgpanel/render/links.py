"""Client links for the web proxy."""

from __future__ import annotations

from urllib.parse import quote


def _query(host: str, secret: str) -> str:
    return f"server={quote(host, safe='')}&secret={quote(secret, safe='')}"


def https_link(host: str, secret: str) -> str:
    return f"https://t.me/webproxy?{_query(host, secret)}"


def tg_link(host: str, secret: str) -> str:
    return f"tg://webproxy?{_query(host, secret)}"
