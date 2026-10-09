"""Patch only the ``limits`` object of the relay's ``config.json``."""

from __future__ import annotations

import json
import re
from typing import Any

from tgpanel.render.errors import RenderError

_INDENT_RE = re.compile(r"^([ \t]+)\S", re.MULTILINE)


def _detect_indent(text: str) -> str | int:
    match = _INDENT_RE.search(text)
    if match is None:
        return 2
    indent = match.group(1)
    return "\t" if indent.startswith("\t") else len(indent)


def patch_config(existing: bytes, limits: dict[str, int]) -> bytes:
    """Return ``existing`` with ``limits`` keys set; everything else is preserved."""
    try:
        text = existing.decode()
        root: Any = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RenderError(f"config.json is not valid JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise RenderError("config.json root must be an object")
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise RenderError(f"limit {key!r} must be an integer")
    if limits:
        current = root.get("limits")
        if current is None:
            current = {}
            root["limits"] = current
        elif not isinstance(current, dict):
            raise RenderError("config.json 'limits' must be an object")
        current.update(limits)
    out = json.dumps(root, indent=_detect_indent(text), ensure_ascii=False)
    if text.endswith("\n") or not text.strip():
        out += "\n"
    return out.encode()
