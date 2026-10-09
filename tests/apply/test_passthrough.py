# ruff: noqa: RUF001
"""Unmanaged ("foreign") profiles of the relay pass through every apply untouched."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.apply.conftest import CONFIG, PROFILES, RELAY, Env, add_users
from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.importer import Importer, RowEdit
from tgpanel.db import repo
from tgpanel.domain.invariants import rendered_profile_count, validate_desired_state
from tgpanel.domain.models import DesiredState, PoolRecord, UserStatus
from tgpanel.domain.pools import ensure_sentinel_capacity
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import render_profiles
from tgpanel.system.ops import HttpResult

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "upstream" / "owner" / "profiles.json"
POOL1 = PoolRecord(1, 2400, 8900)


def original_entries() -> list[dict[str, object]]:
    return list(json.loads(FIXTURE.read_text())["profiles"])


def entries(env: Env) -> list[dict[str, object]]:
    return list(env.fake.get_json(PROFILES)["profiles"])


@pytest.fixture
def owner(make: Callable[..., Env]) -> Env:
    return make("owner")


async def test_declined_import_first_apply_keeps_foreign_profiles(owner: Env) -> None:
    out = await owner.pipeline.run_operation(
        add_users(owner.clock, 1, new_pool=POOL1), reason="create"
    )
    assert out.ok, out.error
    got = entries(owner)
    assert got[0]["name"] == "u1"
    assert got[1:] == original_entries()  # parsed-equal, original order, original backend
    assert {e["backend"] for e in got[1:]} == {"127.0.0.1:2398"}
    assert owner.fake.restart_count(RELAY) == 1 and len(owner.runs()) == 1
    assert owner.fake.calls_of("tproxy_check")
    assert await owner.pipeline.detect_drift() is None
    assert owner.setting("apply.profiles_hash")
    # the relay limit counts the unmanaged ones too (16 profiles -> still the 32 minimum)
    assert json.loads(owner.fake.files[CONFIG].data)["limits"]["max_profiles"] == 32
    # the legacy process is untouched
    assert not [
        c for c in owner.fake.calls_of("systemctl") if c[2] in ("mtproxy", "mtproxy.service")
    ]
    owner.assert_no_secret_leaks(out.error or "")


async def test_no_sentinel_when_only_foreign_profiles_exist(owner: Env) -> None:
    out = await owner.pipeline.apply_now("first")
    assert out.ok
    assert entries(owner) == original_entries()
    assert owner.db.call(repo.list_pools) == []  # no pool is created just for a sentinel
    assert not [u for u in owner.fake.active if u.startswith("tgpanel-mtproxy@")]
    # a user appears and is disabled again: still no sentinel
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="a")
    ).ok

    def disable(conn: object) -> None:
        repo.update_user(conn, 1, status=UserStatus.DISABLED)  # type: ignore[arg-type]

    assert (await owner.pipeline.run_operation(disable, reason="d")).ok
    names = [e["name"] for e in entries(owner)]
    assert "_tgpanel_sentinel" not in names and len(names) == 15
    assert owner.fake.get_text("/etc/tgpanel/mtproxy/1.env").count("-S ") == 1


async def test_limits_count_foreign_profiles(make: Callable[..., Env]) -> None:
    env = make("owner")
    doc: dict[str, list[dict[str, str]]] = {"profiles": []}
    for i in range(40):
        doc["profiles"].append(
            {"name": f"user_{10000 + i}", "secret": f"{i + 1:032x}", "backend": "127.0.0.1:2398"}
        )
    env.fake.files[PROFILES].data = json.dumps(doc).encode()
    out = await env.pipeline.run_operation(add_users(env.clock, 1, new_pool=POOL1), reason="x")
    assert out.ok, out.error
    assert json.loads(env.fake.files[CONFIG].data)["limits"]["max_profiles"] == 41 + 16


async def test_foreign_limits_block_and_odd_fields_are_kept_verbatim(owner: Env) -> None:
    doc = json.loads(FIXTURE.read_text())
    doc["profiles"][2]["limits"] = {"max_sessions": 7}
    doc["profiles"][3]["carrier_mode"] = "websocket"
    doc["profiles"][4]["name"] = "имя с пробелом"
    owner.fake.files[PROFILES].data = json.dumps(doc, ensure_ascii=False).encode()
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="x")
    ).ok
    assert entries(owner)[1:] == doc["profiles"]


async def test_modified_foreign_profile_is_an_external_change(owner: Env) -> None:
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="a")
    ).ok
    doc = owner.fake.get_json(PROFILES)
    secret = doc["profiles"][3]["secret"]
    doc["profiles"][3]["backend"] = "127.0.0.1:2399"
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    tampered = owner.fake.files[PROFILES].data
    out = await owner.pipeline.run_operation(add_users(owner.clock, 1), reason="b")
    assert out.status == "external_change" and not out.ok
    assert out.external is not None and secret not in out.external.description
    assert secret not in (out.error or "")
    assert owner.fake.files[PROFILES].data == tampered
    assert len(owner.users()) == 1
    owner.assert_no_secret_leaks(out.error or "", out.external.description)


async def test_added_and_removed_foreign_profiles_are_reported(owner: Env) -> None:
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="a")
    ).ok
    doc = owner.fake.get_json(PROFILES)
    removed = doc["profiles"].pop(5)["name"]
    doc["profiles"].append({"name": "newcomer", "secret": "c" * 32, "backend": "127.0.0.1:2398"})
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    report = await owner.pipeline.detect_drift()
    assert report is not None
    assert "newcomer" in report.description and removed in report.description
    assert "c" * 32 not in report.description


async def test_change_to_our_own_entry_is_detected(owner: Env) -> None:
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="a")
    ).ok
    doc = owner.fake.get_json(PROFILES)
    doc["profiles"][0]["carrier_mode"] = "websocket"
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    assert await owner.pipeline.detect_drift() is not None


FAULTS: dict[str, Callable[[Env], None]] = {
    "relay_restart": lambda e: e.fake.fail_on("systemctl", f"restart {RELAY}"),
    "write_profiles": lambda e: e.fake.fail_on("write_atomic", PROFILES),
    "healthz": lambda e: e.fake.queue_http("/healthz", [HttpResult(503)] * 3),
    "nft": lambda e: e.fake.fail_on("nft_add_elements"),
    "pool_port": lambda e: e.fake.set_port_open(2400, False),
}


@pytest.mark.parametrize("fault", list(FAULTS))
async def test_failure_rolls_back_with_foreign_profiles_intact(owner: Env, fault: str) -> None:
    before = owner.files()
    active = set(owner.fake.active)
    FAULTS[fault](owner)
    out = await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="x")
    assert not out.ok and out.rolled_back and out.rollback_errors == ()
    assert owner.files() == before
    assert owner.fake.files[PROFILES].data == FIXTURE.read_bytes()
    assert owner.users() == [] and owner.db.call(repo.list_pools) == []
    assert set(owner.fake.active) == active
    assert owner.runs()[0].status == "failed"


async def test_partial_import_via_skip(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    skipped = ["user_12345", "user_99999", "user_20240601"]
    res = await imp.confirm(prev, {name: RowEdit(skip=True) for name in skipped})
    assert res.ok and res.imported == 12 and res.skipped == 3
    got = entries(owner)
    assert [e["name"] for e in got[:12]] == [f"u{i}" for i in range(1, 13)]
    foreign = [e for e in got[12:]]
    assert [e["name"] for e in foreign] == skipped  # original names, original order
    assert all(e["backend"] == "127.0.0.1:2398" for e in foreign)
    originals = {e["name"]: e for e in original_entries()}
    assert foreign == [originals[n] for n in skipped]
    assert {u.source_profile_name for u in owner.users()}.isdisjoint(skipped)
    assert await owner.pipeline.detect_drift() is None
    assert owner.fake.restart_count(RELAY) == 1
    # a second apply changes nothing; the skipped rows are still offered for import
    assert (await owner.pipeline.apply_now("again")).status == "noop"
    again = await imp.preview()
    assert again.importable == 3
    # legacy process must stay while unmanaged profiles still use it
    with pytest.raises(OperationRejected, match="3 профилей"):
        await imp.legacy_mtproxy(False)
    # import the rest: nothing is left on the legacy backend
    assert (await imp.confirm(again)).imported == 3
    assert all(str(e["name"]).startswith("u") for e in entries(owner))
    assert "mtproxy" not in owner.fake.masked
    await imp.legacy_mtproxy(False)
    assert "mtproxy" in owner.fake.masked


async def test_skipping_everything_is_a_noop(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    res = await imp.confirm(prev, {r.row.source_name: RowEdit(skip=True) for r in prev.rows})
    assert res.ok and res.imported == 0 and res.skipped == 15
    assert owner.fake.files[PROFILES].data == FIXTURE.read_bytes()
    assert owner.runs() == []


async def test_deleted_user_entry_is_not_mistaken_for_a_foreign_profile(owner: Env) -> None:
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 2, new_pool=POOL1), reason="a")
    ).ok

    def drop(conn: object) -> None:
        repo.delete_users(conn, [2])  # type: ignore[arg-type]

    assert (await owner.pipeline.run_operation(drop, reason="d")).ok
    names = [e["name"] for e in entries(owner)]
    assert "u2" not in names and names[0] == "u1" and len(names) == 16


async def test_restore_keeps_foreign_profiles(owner: Env) -> None:
    assert (
        await owner.pipeline.run_operation(add_users(owner.clock, 1, new_pool=POOL1), reason="a")
    ).ok
    op2 = await owner.pipeline.run_operation(add_users(owner.clock, 1), reason="b")
    run = owner.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    res = await owner.pipeline.restore_backup(run.backup_path, "system")
    assert res.ok, res.error
    got = entries(owner)
    assert got[0]["name"] == "u1" and got[1:] == original_entries()


# ---------------------------------------------------------------- pure domain / render


def _state(foreign: tuple[str, ...], users: tuple[object, ...] = ()) -> DesiredState:
    return DesiredState(
        users=users,  # type: ignore[arg-type]
        pools=(POOL1,),
        sentinel_secret="e" * 32,
        foreign_profiles=foreign,
    )


def test_domain_rules_with_foreign_profiles() -> None:
    raw = json.dumps({"name": "legacy", "secret": "a" * 32, "backend": "127.0.0.1:2398"})
    state = _state((raw,))
    assert validate_desired_state(state) == []  # no sentinel demanded
    assert rendered_profile_count(state) == 1
    assert ensure_sentinel_capacity([], [], 16, has_foreign=True) is None
    assert ensure_sentinel_capacity([], [], 16) is not None
    names = [p["name"] for p in json.loads(render_profiles(state))["profiles"]]
    assert names == ["legacy"]


@pytest.mark.parametrize("clash", ["_tgpanel_sentinel", "u1"])
def test_name_collisions_are_rejected(clash: str) -> None:
    from tests.domain.helpers import make_user

    raw = json.dumps({"name": clash, "secret": "a" * 32, "backend": "127.0.0.1:2398"})
    state = _state((raw,), (make_user(1),))
    assert any("collides" in e for e in validate_desired_state(state))
    with pytest.raises(RenderError):
        render_profiles(state)
