# ruff: noqa: RUF001
from __future__ import annotations

import json
import sqlite3

from argon2 import PasswordHasher

from tests.ops.conftest import CADDYFILE, DOMAIN, PANEL_PATH, PROFILES, Ops
from tgpanel.render.caddy import has_panel_block

FW_UNIT = "/etc/systemd/system/tgpanel-firewall.service"
PANEL_UNIT = "/etc/systemd/system/tgpanel.service"


def settings(ops: Ops) -> dict[str, str]:
    conn = sqlite3.connect(ops.db)
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()


# ------------------------------------------------------------------------------ doctor


def test_doctor_healthy_install(installed: Ops) -> None:
    ops = installed
    ops.fake.put_file(FW_UNIT, b"x")
    code, out = ops("doctor")
    assert "ОШИБКА" not in out, out
    assert code == 0
    assert "Сертификат панели" in out and "Let's Encrypt" in out


def test_doctor_fails_when_relay_down_and_block_missing(installed: Ops) -> None:
    ops = installed
    ops.fake.set_active("tproxy-server", False)
    ops.fake.put_file(CADDYFILE, "# no panel\n")
    code, out = ops("doctor")
    assert code == 1
    assert "Блок панели в Caddyfile: отсутствует" in out


def test_doctor_cert_expiring_warns_and_missing_fails(installed: Ops) -> None:
    from tests.ops.conftest import good_cert

    installed.fake.set_cert(DOMAIN, good_cert(days=5))
    code, out = installed("doctor")
    assert "[ ВНИМ. ] Сертификат панели" in out and code == 0
    installed.fake.set_cert(DOMAIN, None)
    code, out = installed("doctor")
    assert code == 1 and "сертификат не получен" in out


def test_doctor_reports_dns_and_orphans_and_drift(installed: Ops) -> None:
    installed.fake.set_dns(DOMAIN, a=["198.51.100.7"], aaaa=["2001:db8::1"])
    installed.fake.put_file("/etc/tgpanel/mtproxy/9.env", b"MTP_PORT=2409\n")
    p = json.loads(installed.fake.get_text(PROFILES))
    p["profiles"].append(
        {"name": "alien", "secret": "e" * 32, "backend": "127.0.0.1:2398", "carrier_mode": "https"}
    )
    installed.fake.put_file(PROFILES, json.dumps(p), mode=0o400, group="tproxy")
    _, out = installed("doctor")
    assert "не указывает на этот сервер" in out
    assert "AAAA" in out
    assert "Лишние пулы: неизвестны базе данных: 9" in out
    assert "изменён вне панели" in out


def test_doctor_nft_missing_table(installed: Ops) -> None:
    installed.fake.nft_sets.clear()
    code, out = installed("doctor")
    assert code == 1 and "Таблица nft inet tgpanel: отсутствует" in out


# ------------------------------------------------------------------------------ repair


def test_repair_restores_block_units_and_table(installed: Ops) -> None:
    ops = installed
    original = ops.fake.get_text(CADDYFILE)
    ops.fake.put_file(CADDYFILE, original.split("# >>> tgpanel")[0])
    ops.fake.nft_sets.clear()
    ops.fake.files.pop("/etc/systemd/system/tgpanel-mtproxy@.service", None)
    code, out = ops("repair")
    assert code == 0, out
    assert has_panel_block(ops.fake.get_text(CADDYFILE))
    assert ops.fake.files[FW_UNIT].data
    assert ops.fake.files[PANEL_UNIT].data
    assert ("tgpanel", "up") in ops.fake.nft_sets
    assert "/etc/systemd/system/tgpanel-mtproxy@.service" in ops.fake.files
    assert ("enable", "tgpanel-firewall") in ops.fake.systemctl_calls("enable")
    assert any("pre-repair" in p for p in ops.fake.files if p.startswith("/var/backups/"))


def test_repair_rolls_back_caddy_when_validate_fails(installed: Ops) -> None:
    ops = installed
    stripped = ops.fake.get_text(CADDYFILE).split("# >>> tgpanel")[0]
    ops.fake.put_file(CADDYFILE, stripped)
    ops.fake.fail_check("caddy_validate", "bad config")
    code, out = ops("repair")
    assert code == 1
    assert ops.fake.get_text(CADDYFILE) == stripped
    assert "Блок Caddy: не удалось" in out


def test_repair_unit_failure_restores_previous_files(installed: Ops) -> None:
    ops = installed
    ops.fake.put_file(PANEL_UNIT, b"old unit")
    ops.fake.files.pop(FW_UNIT, None)
    ops.fake.fail_on("systemctl", "daemon-reload", times=1)
    code, out = ops("repair")
    assert code == 1
    assert ops.fake.files[PANEL_UNIT].data == b"old unit"
    assert FW_UNIT not in ops.fake.files
    assert "прежние файлы возвращены" in out


def test_repair_never_touches_upstream_files(installed: Ops) -> None:
    ops = installed
    tracked = {
        p: ops.fake.files[p].data
        for p in (PROFILES, "/etc/tproxy-server/config.json", "/etc/mtproxy/mtproxy.env")
    }
    ops("repair")
    assert {p: ops.fake.files[p].data for p in tracked} == tracked


# ----------------------------------------------------------------------------- uninstall


def test_uninstall_restores_preinstall_and_keeps_caddy_storage(installed: Ops) -> None:
    ops = installed
    original_profiles = next(
        json.loads(
            __import__("tgpanel.system.validation", fromlist=["x"]).parse_tar_gz(
                ops.fake.files[p].data
            )["etc/tproxy-server/profiles.json"]
        )
        for p in ops.fake.files
        if p.endswith("-pre-install.tar.gz")
    )
    ops.fake.put_file(PROFILES, json.dumps({"profiles": []}), mode=0o400, group="tproxy")
    ops.fake.set_active("mtproxy", False)
    cfg = json.loads(ops.fake.get_text("/etc/tproxy-server/config.json"))
    cfg["limits"] = {"max_profiles": 99}
    cfg["added_later"] = "keep-me"
    ops.fake.put_file("/etc/tproxy-server/config.json", json.dumps(cfg), mode=0o640, group="tproxy")
    ops.answers = ["y", "y"]
    code, out = ops("uninstall")
    assert code == 0, out
    assert json.loads(ops.fake.get_text(PROFILES)) == original_profiles
    assert not has_panel_block(ops.fake.get_text(CADDYFILE))
    assert "mtproxy" in ops.fake.active
    restored_cfg = json.loads(ops.fake.get_text("/etc/tproxy-server/config.json"))
    assert restored_cfg["added_later"] == "keep-me"  # only limits come from the archive
    assert restored_cfg.get("limits") != {"max_profiles": 99}
    assert "Что изменится в profiles.json" in out and "исчезнут профили" in out
    assert not any(p.endswith("-pre-install.tar.gz") for p in ops.fake.files)
    assert any(p.endswith("-pre-install-used.tar.gz") for p in ops.fake.files)
    for path in (FW_UNIT, PANEL_UNIT, "/etc/systemd/system/tgpanel-mtproxy@.service"):
        assert path not in ops.fake.files
    assert not ops.fake.nft_sets
    assert "/var/lib/caddy/.local/share/caddy/keep.txt" in ops.fake.files
    assert ops.tools.removed_trees == []
    assert "tgpanel" not in ops.fake.active
    assert ("restart", "caddy") in ops.fake.systemctl_calls()


def test_uninstall_purge_removes_data_trees_but_not_backups_or_caddy(installed: Ops) -> None:
    code, out = installed("uninstall", "--purge", "--yes")
    assert code == 0, out
    assert set(installed.tools.removed_trees) == {
        "/opt/tgpanel",
        "/etc/tgpanel",
        "/var/lib/tgpanel",
    }
    assert any(p.endswith("-pre-install-used.tar.gz") for p in installed.fake.files)
    assert "/var/lib/caddy/.local/share/caddy/keep.txt" in installed.fake.files


def test_uninstall_diff_declined_changes_nothing(installed: Ops) -> None:
    ops = installed
    before = ops.fake.get_text(PROFILES)
    ops.answers = ["y", "n"]
    code, out = ops("uninstall")
    assert code == 1 and "Отменено" in out
    assert ops.fake.get_text(PROFILES) == before
    assert has_panel_block(ops.fake.get_text(CADDYFILE))
    assert ("enable-now", "tgpanel") in ops.fake.systemctl_calls()
    assert any(p.endswith("-pre-install.tar.gz") for p in ops.fake.files)


def _drop_archive(ops: Ops) -> None:
    for p in [p for p in ops.fake.files if "pre-install" in p]:
        del ops.fake.files[p]


def test_uninstall_without_archive_refuses_without_force(installed: Ops) -> None:
    ops = installed
    _drop_archive(ops)
    before = {p: f.data for p, f in ops.fake.files.items() if not p.startswith("/var/backups")}
    code, out = ops("uninstall", "--yes")
    assert code == 1 and "--force" in out
    assert {
        p: f.data for p, f in ops.fake.files.items() if not p.startswith("/var/backups")
    } == before
    assert ops.fake.systemctl_calls("disable-now", "tgpanel") == []


def test_uninstall_force_rebuilds_profiles_through_legacy(installed: Ops) -> None:
    from tests.ops.test_import_helpers import import_two_and_create_one

    ops = installed
    import_two_and_create_one(ops)
    _drop_archive(ops)
    code, out = ops("uninstall", "--force", "--yes")
    assert code == 0, out
    profiles = json.loads(ops.fake.get_text(PROFILES))["profiles"]
    assert profiles
    assert all(p["backend"] == "127.0.0.1:2398" for p in profiles)
    assert not any(p["name"].startswith("u") and p["name"][1:].isdigit() for p in profiles)
    assert "созданные уже в панели: 1" in out
    assert "mtproxy" in ops.fake.active
    assert not has_panel_block(ops.fake.get_text(CADDYFILE))


def test_uninstall_declined(installed: Ops) -> None:
    installed.answers = ["n"]
    code, out = installed("uninstall")
    assert code == 1 and "Отменено" in out
    assert has_panel_block(installed.fake.get_text(CADDYFILE))


def test_uninstall_rolls_back_files_when_relay_unhealthy(installed: Ops) -> None:
    ops = installed
    changed = json.dumps({"profiles": []})
    ops.fake.put_file(PROFILES, changed, mode=0o400, group="tproxy")
    ops.fake.http_responses["/healthz"] = __import__(
        "tgpanel.system.ops", fromlist=["HttpResult"]
    ).HttpResult(503)
    code, out = ops("uninstall", "--yes")
    assert code == 1
    assert ops.fake.get_text(PROFILES) == changed
    assert has_panel_block(ops.fake.get_text(CADDYFILE))
    assert "возвращаю прежние файлы" in out


# ------------------------------------------------------------------------------ update


def test_update_success(installed: Ops) -> None:
    code, out = installed("update", "--ref", "main")
    assert code == 0, out
    t = installed.tools
    assert [
        c[0] for c in t.calls if c[0] in ("git_fetch", "git_checkout", "pip_install_locked")
    ] == [
        "git_fetch",
        "git_checkout",
        "pip_install_locked",
    ]
    assert t.head == "b" * 40
    assert installed.fake.systemctl_calls("stop", "tgpanel")  # stopped during the swap
    assert installed.fake.files["/var/lib/tgpanel/ref"].data == b"main\n"
    assert any("pre-update" in p for p in installed.fake.files)


def test_update_auto_ref_picks_latest_tag(installed: Ops) -> None:
    installed.tools.latest_tag = "v1.2.3"
    installed.tools.refs["v1.2.3"] = "d" * 40
    code, out = installed("update")
    assert code == 0, out
    assert "v1.2.3" in out and installed.tools.head == "d" * 40
    assert installed.fake.files["/var/lib/tgpanel/ref"].data == b"auto\n"


def test_update_auto_ref_without_tags_warns_about_branch(installed: Ops) -> None:
    code, out = installed("update")
    assert code == 0 and "движущаяся ветка main" in out


def test_update_same_commit_is_noop(installed: Ops) -> None:
    installed.tools.refs["main"] = installed.tools.head
    code, out = installed("update", "--ref", "main")
    assert code == 0 and "обновлять нечего" in out
    assert installed.tools.calls_of("git_checkout") == []


def test_update_rolls_back_when_panel_does_not_come_up(installed: Ops) -> None:
    ops = installed
    ops.fake.set_port_open(8090, False)
    code, out = ops("update", "--ref", "main")
    assert code == 1
    assert ops.tools.head == "a" * 40
    assert [c[2] for c in ops.tools.calls_of("git_checkout")] == ["b" * 40, "a" * 40]
    assert "Возвращаю версию" in out


def test_update_rolls_back_on_pip_failure(installed: Ops) -> None:
    installed.tools.fail_on("pip_install_locked", times=1)
    code, _ = installed("update", "--ref", "main")
    assert code == 1 and installed.tools.head == "a" * 40


def test_update_unknown_ref(installed: Ops) -> None:
    code, out = installed("update", "--ref", "nope")
    assert code == 1 and "Не удалось получить обновление" in out


def test_update_rejects_bad_ref(installed: Ops) -> None:
    assert installed("update", "--ref=--evil")[0] == 2
    assert installed("update", "--ref", "a..b")[0] == 2
    assert installed.tools.calls_of("git_fetch") == []


# ------------------------------------------------------------------- passwords and url


def test_reset_password_prints_once_and_stores_hash(installed: Ops) -> None:
    code, out = installed("reset-password")
    assert code == 0
    password = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("Новый"))
    stored = settings(installed)
    assert stored["panel_login"] == "admin"
    assert PasswordHasher().verify(stored["panel_password_hash"], password)
    assert password not in "".join(str(c) for c in installed.fake.calls)
    conn = sqlite3.connect(installed.db)
    try:
        audit = "".join(str(r) for r in conn.execute("SELECT * FROM audit_log"))
    finally:
        conn.close()
    assert password not in audit
    conn = sqlite3.connect(installed.db)
    try:
        v1 = conn.execute("SELECT value FROM settings WHERE key='panel_session_version'").fetchone()
    finally:
        conn.close()
    assert v1 is not None and v1[0] == "1"
    _, out2 = installed("reset-password")
    assert settings(installed)["panel_session_version"] == "2"
    assert password not in out2


def test_bootstrap_keeps_existing_credentials(ops: Ops) -> None:
    import os

    os.environ["TGPANEL_BOOTSTRAP_PASSWORD"] = "first-password-123"
    try:
        _, out = ops("bootstrap", f"--domain={DOMAIN}", "--login=boss", "--admin-id=7")
        first = settings(ops)["panel_password_hash"]
        os.environ["TGPANEL_BOOTSTRAP_PASSWORD"] = "second-password-456"
        _, out2 = ops("bootstrap", f"--domain={DOMAIN}", "--login=other", "--admin-id=7")
    finally:
        del os.environ["TGPANEL_BOOTSTRAP_PASSWORD"]
    assert "credentials-created" in out
    assert "credentials-kept" in out2
    s = settings(ops)
    assert s["panel_password_hash"] == first and s["panel_login"] == "boss"
    assert s["panel_hostname"] == DOMAIN and s["proxy_hostname"] == "proxy.example.com"


def test_show_url(installed: Ops) -> None:
    installed("reset-password")
    code, out = installed("show-url")
    assert code == 0
    assert f"https://{DOMAIN}/{PANEL_PATH}/" in out and "Логин: admin" in out


def test_pre_install_backup_is_created_once(ops: Ops) -> None:
    _, first = ops("pre-install-backup")
    _, second = ops("pre-install-backup")
    assert first == second and first.strip().endswith("-pre-install.tar.gz")
    names = [p for p in ops.fake.files if p.endswith("-pre-install.tar.gz")]
    assert len(names) == 1
    assert ops.fake.files[names[0]].mode == 0o600


def test_bootstrap_does_not_readd_removed_admin(ops: Ops) -> None:
    ops("bootstrap", f"--domain={DOMAIN}", "--admin-id=7")
    conn = sqlite3.connect(ops.db)
    conn.execute("DELETE FROM admins")
    conn.commit()
    conn.close()
    ops("bootstrap", f"--domain={DOMAIN}", "--admin-id=7")
    conn = sqlite3.connect(ops.db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0
    finally:
        conn.close()


# ------------------------------------------------------------------- doctor details


def test_doctor_guard_chain_missing_fails(installed: Ops) -> None:
    installed.tools.chains.discard(("tgpanel", "guard"))
    code, out = installed("doctor")
    assert code == 1 and "Цепочка nft guard" in out and "доступны снаружи" in out


def test_doctor_aaaa_matching_local_ipv6_is_fine_and_mapped_ignored(installed: Ops) -> None:
    ip = installed.fake.public_ip or ""
    installed.tools.ipv6 = ["2001:db8::5"]
    installed.fake.set_dns(DOMAIN, a=[ip], aaaa=["2001:db8::5", "::ffff:203.0.113.10"])
    _, out = installed("doctor")
    assert "DNS домена панели: A →" in out
    installed.fake.set_dns(DOMAIN, a=[ip], aaaa=["2001:db8::99"])
    _, out = installed("doctor")
    assert "AAAA-запись (2001:db8::99)" in out


def test_caddy_env_honours_environment_file(installed: Ops) -> None:
    import asyncio

    from tests.ops.conftest import FAST
    from tgpanel.doctor import read_caddy_env

    installed.fake.put_file(
        "/etc/systemd/system/caddy.service.d/extra.conf",
        "[Service]\nEnvironmentFile=-/etc/caddy/extra.env\n",
    )
    installed.fake.put_file("/etc/caddy/extra.env", "EXTRA_VAR=from-file\n")
    env = asyncio.run(read_caddy_env(installed.fake, FAST))
    assert env["EXTRA_VAR"] == "from-file"
    assert env["TPROXY_HOSTNAME"] == "proxy.example.com"


def test_update_stage2_failure_rolls_back_and_keeps_snapshot(installed: Ops) -> None:
    installed.tools.post_update_rc = 1
    installed.tools.post_update_output = "boom"
    code, out = installed("update", "--ref", "main")
    assert code == 1 and "этап 2 обновления завершился с кодом 1" in out
    assert installed.tools.head == "a" * 40
    assert installed.fake.restart_count("tgpanel") >= 1
    assert any(p.endswith("-pre-update.db") for p in installed.fake.files)


def test_update_refuses_downgrade_without_flag(installed: Ops) -> None:
    installed.tools.ancestors.add(("b" * 40, "a" * 40))
    code, out = installed("update", "--ref", "main")
    assert code == 1 and "--allow-downgrade" in out
    assert installed.tools.calls_of("git_checkout") == []
    code, out = installed("update", "--ref", "main", "--allow-downgrade")
    assert code == 0, out


def test_update_verify_tag_needs_keys_and_signature(installed: Ops) -> None:
    installed.tools.refs["v1.0.0"] = "e" * 40
    code, out = installed("update", "--ref", "v1.0.0", "--verify-tag")
    assert code == 1 and "trusted-signers пуст" in out
    installed.fake.put_file(
        "/opt/tgpanel/deploy/trusted-signers",
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\nxx\n-----END PGP PUBLIC KEY BLOCK-----\n",
    )
    installed.tools.tag_verified = False
    code, out = installed("update", "--ref", "v1.0.0", "--verify-tag")
    assert code == 1 and "подпись тега" in out and installed.tools.calls_of("git_checkout") == []
    installed.tools.tag_verified = True
    code, out = installed("update", "--ref", "v1.0.0", "--verify-tag")
    assert code == 0, out
    code, out = installed("update", "--ref", "main", "--verify-tag", "--force")
    assert code == 1 and "только с тегами" in out


def test_update_prints_full_installed_sha(installed: Ops) -> None:
    _, out = installed("update", "--ref", "main")
    assert "b" * 40 in out


def test_doctor_warns_about_foreign_listener_on_pool_ports(installed: Ops) -> None:
    installed.tools.listeners = [(2410, "nginx"), (2401, "mtproto-proxy"), (80, "caddy")]
    _, out = installed("doctor")
    assert "2410 (nginx)" in out and "2401" not in out.split("заняты посторонними")[1]
