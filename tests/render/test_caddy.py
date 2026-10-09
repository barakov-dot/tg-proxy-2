from __future__ import annotations

import pytest

from tests.render.conftest import fixture_text
from tgpanel.render import caddy
from tgpanel.render.errors import RenderError

PATH = "AbCdEfGhIjKlMnOpQrStUv_-x"
DOM = "panel.example.com"


def test_insert_keeps_rest_and_golden(golden):  # type: ignore[no-untyped-def]
    base = fixture_text("clean", "Caddyfile")
    out = caddy.insert_panel_block(base, DOM, PATH)
    golden("Caddyfile_with_panel", out)
    assert out.startswith(base)
    assert out[len(base) :].startswith("\n# >>> tgpanel\n")
    assert out.endswith("# <<< tgpanel\n")
    assert f"@panel path /{PATH}/*" in out
    assert "log" not in out.split("# >>> tgpanel")[1]
    assert caddy.has_panel_block(out) and not caddy.has_panel_block(base)


def test_idempotent_and_replace() -> None:
    base = fixture_text("owner", "Caddyfile")
    once = caddy.insert_panel_block(base, DOM, PATH)
    assert caddy.insert_panel_block(once, DOM, PATH) == once
    replaced = caddy.insert_panel_block(once, "other.example.org", PATH + "zz")
    assert replaced.startswith(base)
    assert "other.example.org {" in replaced and DOM not in replaced
    assert replaced.count("# >>> tgpanel") == 1


def test_replace_in_middle_keeps_surroundings() -> None:
    head, tail = "a.example.com {\n\trespond 200\n}\n\n", "\nb.example.com {\n\trespond 201\n}\n"
    text = head + caddy.render_panel_block(DOM, PATH) + tail
    out = caddy.insert_panel_block(text, "x.example.com", PATH)
    assert out.startswith(head) and out.endswith(tail)


def test_remove_roundtrip() -> None:
    base = fixture_text("clean", "Caddyfile")
    assert caddy.remove_panel_block(caddy.insert_panel_block(base, DOM, PATH)) == base
    assert caddy.remove_panel_block(base) == base
    assert caddy.remove_panel_block(caddy.insert_panel_block("", DOM, PATH)) == ""


def test_insert_without_trailing_newline() -> None:
    out = caddy.insert_panel_block("a.example.com {\n}", DOM, PATH)
    assert out.startswith("a.example.com {\n}\n\n# >>> tgpanel\n")


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "localhost",
        "a..com",
        "-a.com",
        "a.com\nrespond",
        "pa nel.example.com",
        "x.123",
        "a.b/c",
        "{$X}.com",
        "a" * 64 + ".com",
    ],
)
def test_bad_domain(domain: str) -> None:
    with pytest.raises(RenderError):
        caddy.insert_panel_block("", domain, PATH)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "short",
        "a" * 21,
        "a" * 22 + "/x",
        "a" * 22 + "\n}",
        "/" + "a" * 22,
        "a" * 22 + " b",
        "a" * 22 + "*",
        "é" * 25,
    ],
)
def test_bad_path(path: str) -> None:
    with pytest.raises(RenderError):
        caddy.insert_panel_block("", DOM, path)


def test_malformed_markers() -> None:
    for bad in (
        "# >>> tgpanel\nx\n",
        "# <<< tgpanel\n# >>> tgpanel\n",
        "# >>> tgpanel\n# >>> tgpanel\n# <<< tgpanel\n",
    ):
        with pytest.raises(RenderError):
            caddy.has_panel_block(bad)


def test_domain_normalised_lowercase() -> None:
    assert "panel.example.com {" in caddy.insert_panel_block("", "PANEL.Example.com", PATH)


def test_panel_block_caps_request_body() -> None:
    block = caddy.render_panel_block(DOM, PATH)
    assert "request_body {\n\t\t\tmax_size 2MB\n\t\t}" in block
    assert block.index("request_body") < block.index("reverse_proxy")
