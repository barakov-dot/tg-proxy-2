from __future__ import annotations

import json

import pytest

from tests.render.conftest import fixture_bytes
from tgpanel.render.config import patch_config
from tgpanel.render.errors import RenderError


@pytest.mark.parametrize("variant", ["clean", "owner"])
def test_empty_limits_is_identity(variant: str) -> None:
    raw = fixture_bytes(variant, "config.json")
    assert json.loads(patch_config(raw, {})) == json.loads(raw)
    assert patch_config(raw, {}) == raw  # fixtures use json.dumps(indent=2) style


def test_only_limits_change_and_unknown_keys_survive() -> None:
    raw = fixture_bytes("clean", "config.json")
    out = patch_config(raw, {"max_profiles": 64, "max_sessions_global": 1024, "brand_new": 5})
    before, after = json.loads(raw), json.loads(out)
    assert list(before) == list(after)  # order preserved
    for key in before:
        if key != "limits":
            assert before[key] == after[key]
    for key in ("BasePath", "TokenKeyFile", "StaticRoutes"):
        assert key in after
    lim = after["limits"]
    assert lim["max_profiles"] == 64 and lim["max_sessions_global"] == 1024
    assert lim["brand_new"] == 5
    assert lim["max_pending_global"] == before["limits"]["max_pending_global"]  # untouched
    assert list(lim)[: len(before["limits"])] == list(before["limits"])


def test_limits_created_when_absent() -> None:
    raw = fixture_bytes("owner", "config.json")
    out = patch_config(raw, {"max_profiles": 32})
    after = json.loads(out)
    assert after["limits"] == {"max_profiles": 32}
    assert list(after)[-1] == "limits"
    assert patch_config(out, {"max_profiles": 32}) == out  # idempotent


def test_indent_style_preserved() -> None:
    four = b'{\n    "a": 1,\n    "limits": {\n        "x": 1\n    }\n}\n'
    assert (
        patch_config(four, {"x": 2})
        == b'{\n    "a": 1,\n    "limits": {\n        "x": 2\n    }\n}\n'
    )
    tab = b'{\n\t"a": 1\n}'
    assert patch_config(tab, {"y": 1}) == b'{\n\t"a": 1,\n\t"limits": {\n\t\t"y": 1\n\t}\n}'
    compact = b'{"a":1}'
    assert patch_config(compact, {"y": 1}).startswith(b'{\n  "a": 1')


def test_rejects_bad_input() -> None:
    for bad in (b"[]", b"1", b"nope", b'{"limits": 3}'):
        with pytest.raises(RenderError):
            patch_config(bad, {"a": 1})
    with pytest.raises(RenderError):
        patch_config(b"{}", {"a": "x"})  # type: ignore[dict-item]
    with pytest.raises(RenderError):
        patch_config(b"{}", {"a": True})


def test_non_ascii_preserved() -> None:
    raw = '{\n  "Title": "Прокси",\n  "limits": {}\n}\n'.encode()
    assert "Прокси".encode() in patch_config(raw, {"a": 1})
