"""Manage the tgpanel block inside the Caddyfile (markers per PLAN 3.8)."""

from __future__ import annotations

import re

from tgpanel.render.errors import RenderError

BEGIN_MARKER = "# >>> tgpanel"
END_MARKER = "# <<< tgpanel"

_DOMAIN_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PATH = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_UPSTREAM = re.compile(r"^127\.0\.0\.1:[0-9]{2,5}$")


def validate_domain(domain: str) -> str:
    """Return the normalised (lowercase) domain or raise."""
    value = domain.lower()
    labels = value.split(".")
    if (
        len(value) > 253
        or len(labels) < 2
        or not all(_DOMAIN_LABEL.match(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise RenderError("invalid panel domain")
    return value


def validate_path(random_path: str) -> str:
    if not _PATH.match(random_path):
        raise RenderError("panel path must be 22-128 url-safe characters [A-Za-z0-9_-]")
    return random_path


def render_panel_block(
    panel_domain: str, random_path: str, upstream: str = "127.0.0.1:8090"
) -> str:
    domain = validate_domain(panel_domain)
    path = validate_path(random_path)
    if not _UPSTREAM.match(upstream):
        raise RenderError("panel upstream must be 127.0.0.1:<port>")
    return (
        f"{BEGIN_MARKER}\n"
        f"{domain} {{\n"
        "\tencode zstd gzip\n"
        '\theader Strict-Transport-Security "max-age=31536000"\n'
        f"\t@panel path /{path}/*\n"
        "\thandle @panel {\n"
        "\t\trequest_body {\n"
        "\t\t\tmax_size 2MB\n"
        "\t\t}\n"
        f"\t\treverse_proxy {upstream}\n"
        "\t}\n"
        "\thandle {\n"
        "\t\trespond 404\n"
        "\t}\n"
        "}\n"
        f"{END_MARKER}\n"
    )


def _find_block(caddyfile: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of the marker block incl. trailing newline, or None."""
    begins = [
        m.start() for m in re.finditer(rf"^{re.escape(BEGIN_MARKER)}[ \t]*$", caddyfile, re.M)
    ]
    ends = [m for m in re.finditer(rf"^{re.escape(END_MARKER)}[ \t]*$", caddyfile, re.M)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0].start() < begins[0]:
        raise RenderError("Caddyfile has malformed tgpanel markers")
    end = ends[0].end()
    if caddyfile[end : end + 1] == "\n":
        end += 1
    return begins[0], end


def has_panel_block(caddyfile: str) -> bool:
    return _find_block(caddyfile) is not None


def insert_panel_block(caddyfile: str, panel_domain: str, random_path: str) -> str:
    block = render_panel_block(panel_domain, random_path)
    found = _find_block(caddyfile)
    if found is not None:
        start, end = found
        return caddyfile[:start] + block + caddyfile[end:]
    if not caddyfile:
        return block
    sep = "\n" if caddyfile.endswith("\n") else "\n\n"
    return caddyfile + sep + block


def remove_panel_block(caddyfile: str) -> str:
    found = _find_block(caddyfile)
    if found is None:
        return caddyfile
    start, end = found
    # Drop the single blank separator line that insert_panel_block added before the block.
    if start >= 2 and caddyfile[start - 2 : start] == "\n\n":
        start -= 1
    return caddyfile[:start] + caddyfile[end:]
