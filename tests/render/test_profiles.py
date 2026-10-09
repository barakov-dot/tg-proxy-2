from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.render.conftest import SENTINEL, fixture_bytes, make_state, make_user, secret
from tgpanel.domain.models import CarrierMode, PoolRecord, UserStatus
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import parse_profiles, profiles_hash, render_profiles


def test_only_active_users_and_golden(golden: Callable[[str, bytes], None]) -> None:
    state = make_state(
        (
            make_user(3, mode=CarrierMode.WEBSOCKET),
            make_user(1),
            make_user(2, status=UserStatus.DISABLED),
            make_user(4, pool_id=2, sec="dd" + secret(4)),
            make_user(5, status=UserStatus.EXPIRED),
        ),
        default_carrier_mode=CarrierMode.HTTPS_LANES,
    )
    out = render_profiles(state)
    golden("profiles_active.json", out)
    assert out.endswith(b"\n")
    data = json.loads(out)
    names = [p["name"] for p in data["profiles"]]
    assert names == ["u1", "u3", "u4"]
    by = {p["name"]: p for p in data["profiles"]}
    assert by["u1"]["carrier_mode"] == "https-lanes"
    assert by["u3"]["carrier_mode"] == "websocket"
    assert by["u4"]["secret"] == "dd" + secret(4)  # dd kept in the relay profile
    assert by["u4"]["backend"] == "127.64.0.4:2401"
    assert all("limits" not in p for p in data["profiles"])
    assert list(by["u1"]) == ["name", "secret", "backend", "carrier_mode"]
    assert render_profiles(state) == out


def test_sentinel_when_no_active(golden: Callable[[str, bytes], None]) -> None:
    state = make_state((make_user(1, status=UserStatus.DISABLED),))
    out = render_profiles(state)
    golden("profiles_sentinel.json", out)
    prof = json.loads(out)["profiles"]
    assert prof == [
        {
            "name": "_tgpanel_sentinel",
            "secret": SENTINEL,
            "backend": "127.0.0.1:2400",
            "carrier_mode": "https",
        }
    ]


def test_sentinel_without_pools_fails() -> None:
    state = make_state((), pools=())
    with pytest.raises(RenderError):
        render_profiles(state)


def test_sentinel_ignores_unmanaged_pool() -> None:
    state = make_state(
        (), pools=(PoolRecord(9, 2398, 8888, managed=False), PoolRecord(1, 2400, 8900))
    )
    assert json.loads(render_profiles(state))["profiles"][0]["backend"] == "127.0.0.1:2400"


def test_unknown_pool_fails() -> None:
    with pytest.raises(RenderError):
        render_profiles(make_state((make_user(1, pool_id=7),)))


def test_parse_owner_fixture() -> None:
    entries = parse_profiles(fixture_bytes("owner", "profiles.json"))
    assert len(entries) == 15
    assert entries[0].name == "user_93455874"
    assert all(e.backend == "127.0.0.1:2398" and e.carrier_mode == "https" for e in entries)
    assert not any(e.has_limits for e in entries)


def test_parse_extra_fields_tolerated() -> None:
    entries = parse_profiles(fixture_bytes("extra", "profiles_dd_limits.json"))
    assert entries[0].secret.startswith("dd") and len(entries[0].secret) == 34
    assert entries[1].has_limits and entries[1].carrier_mode == "websocket"


def test_parse_invalid() -> None:
    for bad in (b"not json", b"[]", b'{"profiles": [1]}', b'{"profiles":[{"name":"a"}]}'):
        with pytest.raises(RenderError):
            parse_profiles(bad)


def test_hash_ignores_formatting_but_not_content() -> None:
    raw = fixture_bytes("clean", "profiles.json")
    compact = json.dumps(json.loads(raw)).encode()
    assert profiles_hash(raw) == profiles_hash(compact)
    changed = raw.replace(b"2398", b"2399")
    assert profiles_hash(changed) != profiles_hash(raw)
    assert len(profiles_hash(raw)) == 64
