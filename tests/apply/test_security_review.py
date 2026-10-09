"""Phase-9 security review fixes: profiles.json modes, restore credentials, nft checks."""

from __future__ import annotations

import sqlite3
from typing import Any

from tests.apply.conftest import (
    NFT_FILE,
    POOL_UNIT,
    PROFILES,
    RELAY,
    Env,
    add_users,
)
from tgpanel.db import repo
from tgpanel.system.fake import FakeFile
from tgpanel.system.ops import SystemOpsError


def check_copy_modes(env: Env) -> list[int]:
    return [
        int(c[2])
        for c in env.fake.calls_of("write_atomic")
        if str(c[1]).endswith(".tgpanel-check-profiles.json")
    ]


# ===================================================================================== B2


async def test_b2_pool_unit_written_by_apply_requires_the_firewall(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    lines = env.fake.files[POOL_UNIT].data.decode().splitlines()
    assert "Requires=tgpanel-firewall.service" in lines
    assert "CapabilityBoundingSet=" in lines and "Restart=on-failure" in lines


# ===================================================================================== B3


async def test_b3_wide_profiles_mode_is_tightened_with_a_warning(env: Env) -> None:
    env.fake.files[PROFILES] = FakeFile(env.fake.files[PROFILES].data, 0o640, "root", "tproxy")
    env.fake.clear_calls()
    relay = env.fake.restart_count(RELAY)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.ok, out.error
    f = env.fake.files[PROFILES]
    assert (f.mode, f.owner, f.group) == (0o400, "root", "tproxy")
    assert any("0640" in w and "0400" in w for w in out.warnings)
    assert check_copy_modes(env) == [0o600]  # the -check copy is always 0600
    assert env.fake.restart_count(RELAY) == relay + 1
    # the next apply has nothing to complain about
    again = await env.pipeline.run_operation(add_users(env.clock, 1), reason="y")
    assert again.ok and not any("profiles.json имел права" in w for w in again.warnings)
    assert env.fake.files[PROFILES].mode == 0o400


async def test_b3_owner_and_group_of_the_existing_file_are_kept(env: Env) -> None:
    env.fake.files[PROFILES] = FakeFile(env.fake.files[PROFILES].data, 0o644, "root", "relay")
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert out.ok
    f = env.fake.files[PROFILES]
    assert (f.mode, f.owner, f.group) == (0o400, "root", "relay")


async def test_b3_the_fake_check_is_as_strict_as_the_real_binary(env: Env) -> None:
    for mode in (0o640, 0o604, 0o644, 0o660):
        env.fake.put_file("/srv/fixture/p.json", env.fake.files[PROFILES].data, mode=mode)
        env.fake.put_file(
            "/srv/fixture/c.json", env.fake.files["/etc/tproxy-server/config.json"].data
        )
        result = await env.fake.tproxy_check("/srv/fixture/c.json", "/srv/fixture/p.json")
        assert not result.ok, oct(mode)
    for mode in (0o400, 0o600):
        env.fake.put_file("/srv/fixture/p.json", env.fake.files[PROFILES].data, mode=mode)
        assert (await env.fake.tproxy_check("/srv/fixture/c.json", "/srv/fixture/p.json")).ok


async def test_b3_fresh_install_writes_0400(make: Any) -> None:
    e = make("clean")
    del e.fake.files[PROFILES]
    out = await e.pipeline.apply_now("first", force_external=True)
    assert out.ok and (e.fake.files[PROFILES].mode, e.fake.files[PROFILES].group) == (
        0o400,
        "tproxy",
    )


# ===================================================================================== S1


async def test_s1_restore_does_not_resurrect_credentials(env: Env) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        for key, value in (
            ("panel_login", "admin"),
            ("panel_password_hash", "hash-old"),
            ("panel_session_version", "3"),
            ("bot_token_extra", "old-token"),
            ("timezone", "Europe/Moscow"),
        ):
            repo.set_setting(conn, key, value)
        repo.add_admin(conn, 111, env.clock())

    env.db.call(seed)
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")).ok
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path  # holds the state with login/hash-old/admin 111

    def rotate(conn: sqlite3.Connection) -> None:
        repo.set_setting(conn, "panel_login", "boss")
        repo.set_setting(conn, "panel_password_hash", "hash-new")
        repo.set_setting(conn, "panel_session_version", "4")
        repo.set_setting(conn, "bot_token_extra", "new-token")
        repo.set_setting(conn, "timezone", "UTC")
        repo.remove_admin(conn, 111)
        repo.add_admin(conn, 222, env.clock())

    await env.pipeline.db_write(rotate)
    res = await env.pipeline.restore_backup(run.backup_path, "system")
    assert res.ok, res.error
    settings = env.db.call(repo.all_settings)
    assert settings["panel_login"] == "boss"
    assert settings["panel_password_hash"] == "hash-new"
    assert settings["bot_token_extra"] == "new-token"
    assert settings["panel_session_version"] == "5"  # bumped: old sessions die
    assert settings["timezone"] == "Europe/Moscow"  # ordinary settings ARE restored
    assert env.db.call(repo.list_admins) == [222]  # the admins table is kept as it is
    assert len(env.users()) == 1


async def test_s1_session_version_starts_at_one_when_absent(env: Env) -> None:
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="a")).ok
    op2 = await env.pipeline.run_operation(add_users(env.clock, 1), reason="b")
    run = env.db.call(repo.get_apply_run, op2.apply_run_id)
    assert run and run.backup_path
    assert env.db.call(repo.get_setting, "panel_session_version") is None
    assert (await env.pipeline.restore_backup(run.backup_path, "system")).ok
    assert env.db.call(repo.get_setting, "panel_session_version") == "1"


# ===================================================================================== S10


async def test_s10_nft_file_is_syntax_checked_before_anything_is_written(env: Env) -> None:
    env.fake.clear_calls()
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    calls = env.fake.calls
    i_check = next(i for i, c in enumerate(calls) if c[0] == "nft_check_file")
    i_write = next(i for i, c in enumerate(calls) if c[:2] == ("write_atomic", NFT_FILE))
    assert i_check < i_write
    assert str(calls[i_check][1]).endswith(".tgpanel-check-tgpanel.nft")
    assert not [p for p in env.fake.files if ".tgpanel-check-" in p]


async def test_s10_nft_syntax_error_aborts_before_any_change(env: Env) -> None:
    files = env.files()
    env.fake.fail_check("nft_check_file", "syntax error, unexpected '}'")
    relay = env.fake.restart_count(RELAY)
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert (
        not out.ok and "nft отклонил" in (out.error or "") and "syntax error" in (out.error or "")
    )
    assert env.files() == files and env.users() == []
    assert env.fake.calls_of("systemctl") == [] and env.fake.restart_count(RELAY) == relay
    assert env.fake.calls_of("nft_load_file") == []


async def test_s10_no_nft_check_when_the_nft_file_is_unchanged(env: Env) -> None:
    from tests.apply.conftest import set_status_mut
    from tgpanel.domain.models import UserStatus

    assert (await env.pipeline.run_operation(add_users(env.clock, 2), reason="x")).ok
    env.fake.clear_calls()
    # disabling changes profiles.json but the nft file only loses an IP -> still checked
    assert (
        await env.pipeline.run_operation(set_status_mut([1], UserStatus.DISABLED), reason="d")
    ).ok
    assert len(env.fake.calls_of("nft_check_file")) == 1
    env.fake.clear_calls()

    def comment(conn: sqlite3.Connection) -> None:
        repo.update_user(conn, 2, comment="c")

    assert (await env.pipeline.run_operation(comment, reason="c")).ok
    assert env.fake.calls_of("nft_check_file") == []


# ===================================================================================== nits


async def test_nit_other_nft_errors_abort_without_a_full_reload(env: Env) -> None:
    files = env.files()
    env.fake.fail_on("nft_list_set", exc=SystemOpsError("nft: permission denied"))
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok and "опросить систему" in (out.error or "")
    assert env.fake.calls_of("nft_load_file") == []  # counters are never zeroed by mistake
    assert env.files() == files and env.users() == []


async def test_nit_missing_table_still_triggers_the_full_load(env: Env) -> None:
    env.fake.nft_sets.pop(("tgpanel", "up"))
    env.fake.nft_sets.pop(("tgpanel", "down"))
    assert (await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")).ok
    assert len(env.fake.calls_of("nft_load_file")) == 1


async def test_nit_rollback_tolerates_elements_that_are_already_gone(env: Env) -> None:
    real = env.fake.systemctl
    fired = [False]

    async def vanish_then_fail(action: str, unit: str) -> None:
        if (action, unit) == ("restart", RELAY) and not fired[0]:
            fired[0] = True
            env.fake.nft_sets[("tgpanel", "up")].clear()
            env.fake.nft_sets[("tgpanel", "down")].clear()
            raise SystemOpsError("boom")
        await real(action, unit)

    env.fake.systemctl = vanish_then_fail  # type: ignore[method-assign]
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok and out.rollback_errors == ()
    assert "ВНИМАНИЕ" not in (out.error or "")


async def test_nit_relay_check_message_is_scrubbed_but_shown(env: Env) -> None:
    secret = "ab" * 16
    env.fake.fail_check("tproxy_check", f"limits.max_pending_global too small; secret {secret}")
    out = await env.pipeline.run_operation(add_users(env.clock, 1), reason="x")
    assert not out.ok
    assert "too small" in (out.error or "") and secret not in (out.error or "")
    assert "max_sessions_global" in (out.error or "")
    assert "памяти" not in (out.error or "")
