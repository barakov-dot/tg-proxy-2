# ruff: noqa: RUF001
"""install.sh --check-only against synthetic fake roots (TGPANEL_ROOT_PREFIX)."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

REQUIRED_FILES = [
    "usr/local/bin/tproxy-server",
    "etc/tproxy-server/config.json",
    "etc/tproxy-server/profiles.json",
    "opt/MTProxy/objs/bin/mtproto-proxy",
    "usr/local/bin/caddy",
    "etc/caddy/Caddyfile",
]
UNITS = ["tproxy-server", "mtproxy", "caddy"]


def make_root(base: Path, *, skip: tuple[str, ...] = ()) -> Path:
    root = base / "fakeroot"
    for rel in REQUIRED_FILES:
        if rel in skip:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic\n")
    for unit in UNITS:
        if unit in skip:
            continue
        marker = root / "run" / "active" / unit
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    if "healthz" not in skip:
        (root / "run").mkdir(parents=True, exist_ok=True)
        (root / "run" / "healthz-ok").touch()
    dropin = root / "etc/systemd/system/caddy.service.d/tproxy.conf"
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("[Service]\nEnvironment=TPROXY_HOSTNAME=proxy.example.com\n")
    return root


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def run_install(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {"PATH": os.environ["PATH"], "TGPANEL_ROOT_PREFIX": str(root), "HOME": str(root)}
    return subprocess.run(  # noqa: S603
        [BASH, str(INSTALL), "--check-only", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def test_all_good_exits_zero(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    before = tree_hash(root)
    res = run_install(root)
    assert res.returncode == 0, res.stderr
    assert "ничего не изменено" in res.stdout
    assert tree_hash(root) == before


def test_missing_binary_exits_one_and_changes_nothing(tmp_path: Path) -> None:
    root = make_root(tmp_path, skip=("usr/local/bin/tproxy-server",))
    before = tree_hash(root)
    res = run_install(root)
    assert res.returncode == 1
    assert "нет файла /usr/local/bin/tproxy-server" in res.stderr
    assert "github.com/telegramdesktop/tproxy-server" in res.stderr
    assert tree_hash(root) == before


def test_every_failed_check_is_reported(tmp_path: Path) -> None:
    root = make_root(
        tmp_path,
        skip=("etc/caddy/Caddyfile", "opt/MTProxy/objs/bin/mtproto-proxy", "mtproxy", "healthz"),
    )
    before = tree_hash(root)
    res = run_install(root)
    assert res.returncode == 1
    for needle in (
        "/etc/caddy/Caddyfile",
        "/opt/MTProxy/objs/bin/mtproto-proxy",
        "служба mtproxy не активна",
        "/healthz не отвечает",
    ):
        assert needle in res.stderr
    assert "служба caddy не активна" not in res.stderr
    assert tree_hash(root) == before


def test_empty_root_reports_everything(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    res = run_install(root)
    assert res.returncode == 1
    assert res.stderr.count("нет файла") == len(REQUIRED_FILES)
    assert list(root.iterdir()) == []


def test_panel_domain_equal_to_proxy_host_is_rejected(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    res = run_install(root, "--panel-domain", "proxy.example.com")
    assert res.returncode == 1
    assert "совпадает с именем хоста прокси" in res.stderr


def test_panel_domain_different_passes(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert run_install(root, "--panel-domain", "panel.example.com").returncode == 0


def test_unknown_argument_fails(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert run_install(root, "--bogus").returncode == 1
