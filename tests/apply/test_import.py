from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.apply.conftest import PROFILES, RELAY, Env, add_users, assert_only_allowed_writes
from tgpanel.apply.importer import OLD_BOT_WARNING, Importer, RowEdit
from tgpanel.db import repo
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.render.links import https_link

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upstream"
ENV1 = "/etc/tgpanel/mtproxy/1.env"


def original_profiles(variant: str) -> list[dict[str, str]]:
    doc = json.loads((FIXTURES / variant / "profiles.json").read_text())
    return list(doc["profiles"])


@pytest.fixture
def owner(make: Callable[..., Env]) -> Env:
    return make("owner")


@pytest.fixture
def clean(make: Callable[..., Env]) -> Env:
    return make("clean")


async def test_preview_owner_fixture(owner: Env) -> None:
    prev = await Importer(owner.pipeline).preview()
    assert len(prev.rows) == 15 and prev.importable == 15 and not prev.blocked
    assert OLD_BOT_WARNING in prev.warnings
    first = prev.rows[0]
    assert first.row.source_name == "user_93455874" and first.row.tg_id == 93455874
    assert first.row.name == "user_93455874" and first.row.display_name == ""
    assert first.pool_id == 1 and first.pool_port == 2400 and first.new_pool
    ips = [r.loopback_ip for r in prev.rows]
    assert ips[0] == "127.64.0.1" and len(set(ips)) == 15
    assert {r.pool_id for r in prev.rows} == {1}
    assert prev.plan.unused_mtproxy_secrets == ()
    # preview is read-only
    assert owner.fake.calls_of("systemctl") == [] and owner.users() == []
    assert owner.runs() == []


async def test_confirm_owner_fixture(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    src = original_profiles("owner")
    old_profiles_bytes = owner.fake.files[PROFILES].data
    old_state = (set(owner.fake.active), owner.fake.restart_count("mtproxy"))
    res = await imp.confirm(await imp.preview(), actor="system")
    assert res.ok and res.imported == 15 and res.error is None

    users = owner.users()
    assert len(users) == 15
    by_secret = {u.secret: u for u in users}
    for p in src:
        u = by_secret[p["secret"]]  # secrets kept as they were
        assert u.imported and u.comment == "import" and u.status is UserStatus.ACTIVE
        assert u.expires_at is None and u.source_profile_name == p["name"]
        assert u.name == p["name"] and u.tg_id == int(p["name"].removeprefix("user_"))
        assert u.pool_id == 1
    extra = owner.db.call(repo.get_user_extra, users[0].id)
    assert extra and extra.first_seen_at is None and extra.last_seen_at is None

    new = owner.fake.get_json(PROFILES)["profiles"]
    assert len(new) == 15
    assert [p["name"] for p in new] == [f"u{u.id}" for u in users]
    assert {p["backend"] for p in new} == {f"{u.loopback_ip}:2400" for u in users}
    assert {p["secret"] for p in new} == {p["secret"] for p in src}
    assert all(p["carrier_mode"] == "https" and "limits" not in p for p in new)
    env_body = owner.fake.get_text(ENV1)
    assert env_body.count("-S ") == 15
    assert "tgpanel-mtproxy@1" in owner.fake.active and owner.fake.port_is_open(2400)
    assert set(owner.fake.nft_sets[("tgpanel", "up")]) == {u.loopback_ip for u in users}
    assert owner.fake.restart_count(RELAY) == 1
    assert len(owner.runs()) == 1
    # old links unchanged: same host + same secret
    for p in src:
        link = https_link("proxy.example.com", by_secret[p["secret"]].secret)
        assert link.endswith(f"secret={p['secret']}")
    # the legacy process is untouched
    assert (set(owner.fake.active) - {"tgpanel-mtproxy@1"}) == old_state[0]
    assert owner.fake.restart_count("mtproxy") == old_state[1]
    assert not any(c[2] in ("mtproxy", "mtproxy.service") for c in owner.fake.calls_of("systemctl"))
    assert owner.fake.files[PROFILES].data != old_profiles_bytes
    assert await owner.pipeline.detect_drift() is None
    assert_only_allowed_writes(owner.fake)
    owner.assert_no_secret_leaks(*(res.error or "",))


async def test_import_is_idempotent(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    assert (await imp.confirm(await imp.preview())).imported == 15
    runs = len(owner.runs())
    relay = owner.fake.restart_count(RELAY)
    prev = await imp.preview()
    assert prev.importable == 0 and all(r.row.skip_reason for r in prev.rows)
    again = await imp.confirm(prev)
    assert again.ok and again.imported == 0 and again.skipped == 15
    assert len(owner.runs()) == runs and owner.fake.restart_count(RELAY) == relay
    assert len(owner.users()) == 15


async def test_pool_up_and_port_open_before_profiles_and_relay(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    await imp.confirm(await imp.preview())
    calls = owner.fake.calls
    i_start = next(
        i for i, c in enumerate(calls) if c[:3] == ("systemctl", "enable-now", "tgpanel-mtproxy@1")
    )
    i_wait = next(i for i, c in enumerate(calls) if c[0] == "wait_tcp_open" and c[2] == 2400)
    i_prof = next(i for i, c in enumerate(calls) if c[0] == "write_atomic" and c[1] == PROFILES)
    i_relay = next(i for i, c in enumerate(calls) if c[:3] == ("systemctl", "restart", RELAY))
    assert i_start < i_wait < i_prof < i_relay


FAULTS: dict[str, Callable[[Env], None]] = {
    "relay_restart": lambda e: e.fake.fail_on("systemctl", f"restart {RELAY}"),
    "healthz": lambda e: e.fake.queue_http(
        "/healthz", [__import__("tgpanel.system.ops", fromlist=["x"]).HttpResult(503)] * 3
    ),
    "write_profiles": lambda e: e.fake.fail_on("write_atomic", PROFILES),
    "pool_start": lambda e: e.fake.fail_on("systemctl", "enable-now tgpanel-mtproxy@1"),
    "pool_port": lambda e: e.fake.set_port_open(2400, False),
    "nft": lambda e: e.fake.fail_on("nft_add_elements"),
    "check": lambda e: e.fake.fail_check("tproxy_check"),
}


@pytest.mark.parametrize("fault", list(FAULTS))
async def test_import_failure_restores_original_behaviour(owner: Env, fault: str) -> None:
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    files_before = owner.files()
    active_before = set(owner.fake.active)
    FAULTS[fault](owner)
    res = await imp.confirm(prev)
    assert not res.ok and res.error
    assert owner.files() == files_before  # original profiles.json byte-identical
    assert owner.fake.files[PROFILES].data == (FIXTURES / "owner" / "profiles.json").read_bytes()
    assert owner.users() == [] and owner.db.call(repo.list_pools) == []
    assert set(owner.fake.active) == active_before
    assert not owner.fake.port_is_open(2400) or fault == "pool_port"
    if fault == "pool_port":
        assert not [c for c in owner.fake.calls_of("write_atomic") if c[1] == PROFILES]
    assert owner.runs()[0].status == "failed"


async def test_import_clean_fixture_default_profile(clean: Env) -> None:
    imp = Importer(clean.pipeline)
    prev = await imp.preview()
    assert prev.importable == 1 and not prev.blocked
    assert prev.rows[0].row.tg_id is None
    assert any("telegram id not recognized" in w for w in prev.warnings)
    assert prev.plan.unused_mtproxy_secrets == ()  # MTPROXY_SECRET matches the profile
    res = await imp.confirm(prev)
    assert res.ok and res.imported == 1
    (user,) = clean.users()
    assert user.name == "default" and user.tg_id is None and user.imported
    assert user.secret == "1e3b4842bde9088cc96a5fafa7fc134e"
    assert clean.fake.get_json(PROFILES)["profiles"][0]["name"] == f"u{user.id}"
    assert clean.fake.get_json(PROFILES)["profiles"][0]["backend"] == "127.64.0.1:2400"


async def test_dd_prefix_is_kept_in_profile_and_stripped_for_mtproxy(clean: Env) -> None:
    base = "1e3b4842bde9088cc96a5fafa7fc134e"
    doc = {"profiles": [{"name": "default", "secret": "dd" + base, "backend": "127.0.0.1:2398"}]}
    clean.fake.files[PROFILES].data = json.dumps(doc).encode()
    imp = Importer(clean.pipeline)
    res = await imp.confirm(await imp.preview())
    assert res.ok
    assert clean.fake.get_json(PROFILES)["profiles"][0]["secret"] == "dd" + base
    body = clean.fake.get_text(ENV1)
    assert f"-S {base}" in body and "dd" + base not in body
    (user,) = clean.users()
    assert user.secret == "dd" + base
    assert https_link("h", user.secret).endswith("secret=dd" + base)


async def test_reconciliation_warnings_and_no_full_secret_in_output(owner: Env) -> None:
    secrets_path = "/etc/mtproxy/mtproxy.secrets"
    lines = owner.fake.get_text(secrets_path).splitlines()
    missing = lines[0]
    stray = "ffffffffffffffffffffffffffffffff"
    owner.fake.files[secrets_path].data = ("\n".join([*lines[1:], stray]) + "\n").encode()
    prev = await Importer(owner.pipeline).preview()
    assert any("probably broken" in w for w in prev.warnings)
    assert any("unused" in w for w in prev.warnings)
    assert prev.importable == 15  # still imported
    assert prev.plan.unused_mtproxy_secrets == ("ffff...",)
    text = "\n".join(prev.warnings + prev.errors)
    assert stray not in text and missing not in text


async def test_blocked_on_duplicate_secret(owner: Env) -> None:
    doc = json.loads(owner.fake.files[PROFILES].data)
    doc["profiles"][1]["secret"] = doc["profiles"][0]["secret"]
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    assert prev.blocked
    res = await imp.confirm(prev)
    assert not res.ok and owner.users() == [] and owner.runs() == []


async def test_csv_and_edits(owner: Env) -> None:
    csv = "user_12345;555000111;Vasya;vip\n"
    imp = Importer(owner.pipeline)
    prev = await imp.preview(csv_text=csv)
    row = next(r.row for r in prev.rows if r.row.source_name == "user_12345")
    assert (row.tg_id, row.display_name, row.comment) == (555000111, "Vasya", "import; vip")
    edits = {"user_93455874": RowEdit(tg_id=777, display_name="Petya", comment="boss")}
    res = await imp.confirm(prev, edits)
    assert res.ok
    by_src = {u.source_profile_name: u for u in owner.users()}
    # the technical name always stays the source profile name; the CSV/edit sets display_name
    assert by_src["user_12345"].name == "user_12345"
    assert by_src["user_12345"].display_name == "Vasya" and by_src["user_12345"].tg_id == 555000111
    assert by_src["user_93455874"].name == "user_93455874"
    assert by_src["user_93455874"].display_name == "Petya" and by_src["user_93455874"].tg_id == 777
    assert by_src["user_93455874"].comment == "import; boss"


async def test_edit_creating_duplicate_id_blocks(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    res = await imp.confirm(prev, {"user_12345": RowEdit(tg_id=93455874)})
    assert not res.ok and "duplicate telegram id" in (res.error or "")
    assert owner.users() == []


async def test_name_conflict_with_existing_user(clean: Env) -> None:
    assert (await clean.pipeline.apply_now("init", force_external=True)).ok

    # a panel user already named "default"
    def mk(conn):  # type: ignore[no-untyped-def]
        ids = add_users(clean.clock, 1, new_pool=PoolRecord(1, 2400, 8900))(conn)
        repo.update_user(conn, ids[0], name="default")

    assert (await clean.pipeline.run_operation(mk, reason="x")).ok
    # restore the legacy profile as the relay state (foreign) and try to import
    clean.fake.files[PROFILES].data = (FIXTURES / "clean" / "profiles.json").read_bytes()
    prev = await Importer(clean.pipeline).preview()
    assert prev.blocked and any("уже занято" in e for e in prev.errors)


@pytest.mark.parametrize(("spp", "pools"), [(16, 1), (15, 1), (14, 2), (5, 3)])
async def test_layout_follows_secrets_per_process(owner: Env, spp: int, pools: int) -> None:
    owner.set_setting("secrets_per_process", str(spp))
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    assert len({r.pool_id for r in prev.rows}) == pools
    res = await imp.confirm(prev)
    assert res.ok, res.error
    counts = repo.pool_occupancy(owner.db.conn)
    assert max(counts.values()) <= spp and sum(counts.values()) == 15
    assert len(owner.db.call(repo.list_pools)) == pools


async def test_spp_15_next_user_opens_second_pool(owner: Env) -> None:
    owner.set_setting("secrets_per_process", "15")
    imp = Importer(owner.pipeline)
    assert (await imp.confirm(await imp.preview())).ok
    from tgpanel.domain.pools import allocate_pools

    alloc = allocate_pools(owner.db.call(repo.list_pools), owner.users(), 1, 15)
    assert len(alloc.new_pools) == 1


async def test_external_change_with_baseline_is_a_warning_in_preview(owner: Env) -> None:
    assert (await owner.pipeline.apply_now("seed", force_external=True)).ok
    doc = owner.fake.get_json(PROFILES)
    doc["profiles"].append({"name": "late", "secret": "a" * 32, "backend": "127.0.0.1:2398"})
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    prev = await Importer(owner.pipeline).preview()
    assert prev.drift and "late" in prev.drift
    assert any("вне панели" in w for w in prev.warnings)
    assert prev.importable == 16  # the 15 pass-through profiles plus the new foreign one
    res = await Importer(owner.pipeline).confirm(prev)
    assert res.ok
    assert "late" in [u.source_profile_name for u in owner.users()]


async def test_profiles_changed_between_preview_and_confirm(owner: Env) -> None:
    imp = Importer(owner.pipeline)
    prev = await imp.preview()
    doc = json.loads(owner.fake.files[PROFILES].data)
    doc["profiles"].append({"name": "late", "secret": "b" * 32, "backend": "127.0.0.1:2398"})
    owner.fake.files[PROFILES].data = json.dumps(doc).encode()
    res = await imp.confirm(prev)
    assert not res.ok and "изменился" in (res.error or "")
    assert owner.users() == []


async def test_unreadable_profiles_is_a_clean_error(make: Callable[..., Env]) -> None:
    from tgpanel.apply.importer import ImportSourceError

    env = make("owner")
    del env.fake.files[PROFILES]
    with pytest.raises(ImportSourceError):
        await Importer(env.pipeline).preview()


async def test_sentinel_profile_is_not_imported(env: Env) -> None:
    prev = await Importer(env.pipeline).preview()
    assert prev.rows == () and prev.plan.sentinel_ignored
    res = await Importer(env.pipeline).confirm(prev)
    assert res.ok and res.imported == 0
