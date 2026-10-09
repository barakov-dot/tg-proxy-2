from __future__ import annotations

import json
from collections.abc import Callable

from tests.apply.conftest import PROFILES, Env, add_users

FOREIGN_SECRET = "abcdefabcdefabcdefabcdefabcdefab"


async def test_foreign_profiles_without_baseline_are_not_drift(make: Callable[..., Env]) -> None:
    env = make("clean")
    assert await env.pipeline.detect_drift() is None
    out = await env.pipeline.apply_now("first")  # no force needed: foreign entries pass through
    assert out.ok
    assert env.setting("apply.profiles_hash")
    assert await env.pipeline.detect_drift() is None


async def test_force_external_overwrites_and_sets_baseline(make: Callable[..., Env]) -> None:
    env = make("clean")
    out = await env.pipeline.apply_now("x", force_external=True)
    assert out.ok
    assert await env.pipeline.detect_drift() is None
    assert env.setting("apply.profiles_hash")


async def test_external_edit_after_baseline_is_detected_not_overwritten(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    doc = env.fake.get_json(PROFILES)
    doc["profiles"].append(
        {
            "name": "sneaky",
            "secret": FOREIGN_SECRET,
            "backend": "127.0.0.1:2398",
            "carrier_mode": "https",
        }
    )
    tampered = json.dumps(doc).encode()
    env.fake.files[PROFILES].data = tampered
    env.fake.clear_calls()

    report = await env.pipeline.detect_drift()
    assert report is not None and not report.no_baseline
    assert "sneaky" in report.description and FOREIGN_SECRET not in report.description

    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.status == "external_change" and not out.ok
    assert FOREIGN_SECRET not in (out.error or "") + repr(out.external)
    assert env.fake.files[PROFILES].data == tampered
    assert len(env.users()) == 2
    env.assert_no_secret_leaks(out.error or "", report.description)


async def test_removed_profile_is_reported(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    doc = env.fake.get_json(PROFILES)
    doc["profiles"] = doc["profiles"][:1]
    env.fake.files[PROFILES].data = json.dumps(doc).encode()
    report = await env.pipeline.detect_drift()
    assert report is not None and "u2" in report.description


async def test_changed_secret_is_reported_without_leaking_it(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    doc = env.fake.get_json(PROFILES)
    doc["profiles"][0]["secret"] = FOREIGN_SECRET
    env.fake.files[PROFILES].data = json.dumps(doc).encode()
    report = await env.pipeline.detect_drift()
    assert report is not None and FOREIGN_SECRET not in report.description


async def test_formatting_only_change_is_not_drift(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 2), reason="seed")
    doc = env.fake.get_json(PROFILES)
    env.fake.files[PROFILES].data = json.dumps(doc, indent=8, sort_keys=True).encode()
    assert await env.pipeline.detect_drift() is None
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.ok


async def test_force_external_overrides_drift(env: Env) -> None:
    await env.pipeline.run_operation(add_users(env.clock, 1), reason="seed")
    doc = env.fake.get_json(PROFILES)
    doc["profiles"][0]["secret"] = FOREIGN_SECRET  # someone edited OUR entry
    env.fake.files[PROFILES].data = json.dumps(doc).encode()
    assert await env.pipeline.detect_drift() is not None
    out = await env.pipeline.apply_now("fix", force_external=True)
    assert out.ok
    assert [p["name"] for p in env.fake.get_json(PROFILES)["profiles"]] == ["u1"]
    assert FOREIGN_SECRET not in env.fake.get_text(PROFILES)
    assert await env.pipeline.detect_drift() is None


async def test_corrupt_profiles_json_is_drift(env: Env) -> None:
    env.fake.files[PROFILES].data = b"{not json"
    report = await env.pipeline.detect_drift()
    assert report is not None
    out = await env.pipeline.apply_now("x")
    assert out.status == "external_change"


async def test_missing_profiles_json_is_recreated(env: Env) -> None:
    del env.fake.files[PROFILES]
    out = await env.pipeline.apply_now("x")
    assert out.ok and PROFILES in env.fake.files


async def test_only_forced_submissions_proceed_when_drifted(env: Env) -> None:
    import asyncio

    env.fake.files[PROFILES].data = (
        b'{"profiles":[{"name":"x","secret":"%s","backend":"127.0.0.1:1"}]}'
        % FOREIGN_SECRET.encode()
    )
    a, b = await asyncio.gather(
        env.pipeline.run_operation(add_users(env.clock, 1), reason="plain"),
        env.pipeline.run_operation(add_users(env.clock, 1), reason="forced", force_external=True),
    )
    assert a.status == "external_change" and b.ok
    assert len(env.users()) == 1
