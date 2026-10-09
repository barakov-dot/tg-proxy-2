"""install.sh behaviours with stubbed system tools in PATH and TGPANEL_ROOT_PREFIX."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.shell.test_install_check import BASH, INSTALL, make_root, tree_hash

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"
PUBLIC_IP = "203.0.113.10"


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_stubs(base: Path, *, a: str = PUBLIC_IP, aaaa: str = "", ss_443: str = "caddy") -> Path:
    stubs = base / "stubs"
    stubs.mkdir()
    log = base / "calls.log"
    write_stub(
        stubs,
        "curl",
        f'''printf '%s\\n' "$*" >> "{log}.curl-argv"
case "$*" in
  *"-K -"*) cat > "{log}.curl-stdin"; printf '{{"ok":true,"result":{{"username":"stubbot"}}}}' ;;
  *) printf '%s' "{PUBLIC_IP}" ;;
esac
''',
    )
    write_stub(
        stubs,
        "getent",
        f'''case "$1" in
  ahostsv4) [ -n "{a}" ] && printf '%s STREAM x\\n' "{a}" ;;
  ahostsv6) [ -n "{aaaa}" ] && printf '%s STREAM x\\n' "{aaaa}" ;;
esac
exit 0
''',
    )
    write_stub(
        stubs,
        "ss",
        f"""case "$*" in
  *ltnp*) printf 'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("caddy",pid=1,fd=7))\\n'
        printf 'LISTEN 0 4096 *:443 *:* users:(("{ss_443}",pid=1,fd=8))\\n' ;;
  *) : ;;
esac
""",
    )
    write_stub(stubs, "ip", "exit 0\n")
    for tool in ("apt-get", "systemctl", "git", "nft"):
        write_stub(stubs, tool, f'echo "{tool} $*" >> "{log}.forbidden"; exit 1\n')
    return stubs


def run(
    root: Path,
    stubs: Path | None,
    *args: str,
    env: dict[str, str] | None = None,
    check_only: bool = True,
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    path = os.environ["PATH"] if stubs is None else f"{stubs}:{os.environ['PATH']}"
    full_env = {
        "PATH": path,
        "TGPANEL_ROOT_PREFIX": str(root),
        "HOME": str(root),
        "TGPANEL_FORCE_NET_CHECKS": "1" if stubs is not None else "",
        **(env or {}),
    }
    cmd = [BASH, str(INSTALL), *(["--check-only"] if check_only else []), *args]
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # no controlling terminal: /dev/tty is unavailable
    )


# ------------------------------------------------------------------------------ B1


def test_random_token_never_starts_with_dash_or_underscore() -> None:
    assert BASH is not None
    script = (
        f'TGPANEL_SOURCE_ONLY=1 source "{INSTALL}"; '
        "for i in $(seq 1 150); do random_token 32; echo; random_token 22; echo; done"
    )
    out = subprocess.run(  # noqa: S603
        [BASH, "-c", script], capture_output=True, text=True, check=True, timeout=120
    ).stdout.split()
    assert len(out) == 300
    assert all(t[0].isalnum() for t in out)
    assert {len(t) for t in out} == {22, 32}
    assert all(
        set(t) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for t in out
    )


# ------------------------------------------------------------------------------ B2


def _update_root(tmp_path: Path) -> Path:
    root = make_root(tmp_path, skip=("mtproxy",))
    (root / "etc/tgpanel").mkdir(parents=True)
    (root / "etc/tgpanel/tgpanel.env").write_text("TGPANEL_PANEL_DOMAIN=panel.example.com\n")
    return root


def test_fresh_install_still_requires_legacy_mtproxy(tmp_path: Path) -> None:
    root = make_root(tmp_path, skip=("mtproxy",))
    res = run(root, None)
    assert res.returncode == 1 and "служба mtproxy не активна" in res.stderr


def test_update_fails_when_mtproxy_inactive_without_reason(tmp_path: Path) -> None:
    res = run(_update_root(tmp_path), None)
    assert res.returncode == 1 and "служба mtproxy не активна" in res.stderr


def test_update_ok_when_a_pool_is_active(tmp_path: Path) -> None:
    root = _update_root(tmp_path)
    (root / "run/active/tgpanel-mtproxy@1").touch()
    assert run(root, None).returncode == 0


def test_update_ok_when_legacy_turned_off_on_purpose(tmp_path: Path) -> None:
    root = _update_root(tmp_path)
    dropin = root / "etc/systemd/system/mtproxy.service.d/tgpanel-off.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Unit]\nConditionPathExists=/nonexistent-tgpanel-disabled\n")
    assert run(root, None).returncode == 0


def test_update_ok_when_legacy_masked(tmp_path: Path) -> None:
    root = _update_root(tmp_path)
    unit = root / "etc/systemd/system/mtproxy.service"
    unit.symlink_to("/dev/null")
    assert run(root, None).returncode == 0


# ------------------------------------------------------------------------- S1 / S2


def test_yes_does_not_skip_dns_check(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path, a="198.51.100.7")
    res = run(root, stubs, "--panel-domain", "panel.example.com", "--yes")
    assert res.returncode == 1
    assert "не указывает на этот сервер" in res.stderr
    ok = run(root, stubs, "--panel-domain", "panel.example.com", "--ignore-dns")
    assert ok.returncode == 0, ok.stderr


def test_v4_mapped_aaaa_is_ignored(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path, aaaa=f"::ffff:{PUBLIC_IP}")
    res = run(root, stubs, "--panel-domain", "panel.example.com", "--yes")
    assert res.returncode == 0, res.stderr


def test_real_foreign_aaaa_is_rejected(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path, aaaa="2001:db8::1")
    res = run(root, stubs, "--panel-domain", "panel.example.com", "--yes")
    assert res.returncode == 1 and "AAAA-запись 2001:db8::1" in res.stderr


def test_port_443_must_belong_to_caddy(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path, ss_443="nginx")
    res = run(root, stubs, "--panel-domain", "panel.example.com")
    assert res.returncode == 1 and "занят не Caddy" in res.stderr


# ---------------------------------------------------------------------------- S3


def test_no_tty_requires_yes_before_any_change(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path)
    before = tree_hash(root)
    res = run(
        root, stubs, "--panel-domain", "panel.example.com", "--admin-id", "1",
        env={"TGPANEL_INSTALL_BOT_TOKEN": TOKEN}, check_only=False,
    )  # fmt: skip
    assert res.returncode == 1 and "нет терминала" in res.stderr
    assert tree_hash(root) == before
    assert not (tmp_path / "calls.log.forbidden").exists()


def test_no_tty_requires_explicit_import_decision(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path)
    before = tree_hash(root)
    res = run(
        root, stubs, "--panel-domain", "panel.example.com", "--admin-id", "1", "--yes",
        env={"TGPANEL_INSTALL_BOT_TOKEN": TOKEN}, check_only=False,
    )  # fmt: skip
    assert res.returncode == 1 and "--import или --no-import" in res.stderr
    assert tree_hash(root) == before
    assert not (tmp_path / "calls.log.forbidden").exists()


def test_missing_value_without_tty_is_a_clean_error(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path)
    res = run(root, stubs, "--yes", "--no-import", check_only=False)
    assert res.returncode == 1 and "нужны --panel-domain" in res.stderr
    assert "сбой на строке" not in res.stderr


# ---------------------------------------------------------------------------- S4


def test_bot_token_is_not_in_curl_argv(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    stubs = make_stubs(tmp_path)
    token_file = tmp_path / "bot.token"
    token_file.write_text(TOKEN + "\n")
    res = run(
        root, stubs, "--panel-domain", "panel.example.com", "--bot-token-file", str(token_file)
    )
    assert res.returncode == 0, res.stderr
    assert TOKEN not in (tmp_path / "calls.log.curl-argv").read_text()
    assert TOKEN in (tmp_path / "calls.log.curl-stdin").read_text()


def test_unknown_argument_does_not_echo_value(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    res = run(root, None, "--bogus=SUPERSECRETVALUE")
    assert res.returncode == 1
    assert "SUPERSECRETVALUE" not in res.stderr + res.stdout
    assert "--bogus" in res.stderr
