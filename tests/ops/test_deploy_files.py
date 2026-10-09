"""deploy/ stays in sync with the code that renders the same files."""

from __future__ import annotations

from pathlib import Path

from tgpanel.render.caddy import render_panel_block
from tgpanel.render.nft import render_firewall_unit

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def test_firewall_unit_matches_renderer() -> None:
    assert (DEPLOY / "tgpanel-firewall.service").read_bytes() == render_firewall_unit()


def test_caddy_template_matches_renderer() -> None:
    domain, path = "panel.example.com", "A" * 22
    text = (
        (DEPLOY / "Caddyfile.block.tmpl")
        .read_text()
        .replace("{{PANEL_DOMAIN}}", domain)
        .replace("{{PANEL_PATH}}", path)
    )
    assert text == render_panel_block(domain, path)


def test_panel_service_essentials() -> None:
    text = (DEPLOY / "tgpanel.service").read_text()
    for needle in (
        "EnvironmentFile=/etc/tgpanel/tgpanel.env",
        "ExecStart=/opt/tgpanel/.venv/bin/python -m tgpanel.main",
        "WorkingDirectory=/opt/tgpanel",
        "Restart=on-failure",
        "NoNewPrivileges=true",
    ):
        assert needle in text
    for rw in ("/etc/tgpanel", "/etc/caddy", "/var/lib/tgpanel", "/var/backups/tgpanel", "/run"):
        assert rw in text


def test_refresh_units_point_at_script() -> None:
    assert "/etc/mtproxy/proxy-multi.conf" in (DEPLOY / "tgpanel-mtproxy-refresh.path").read_text()
    svc = (DEPLOY / "tgpanel-mtproxy-refresh.service").read_text()
    assert "ExecStart=/opt/tgpanel/scripts/restart-pools.sh" in svc


def _rw_paths() -> list[str]:
    paths: list[str] = []
    for line in (DEPLOY / "tgpanel.service").read_text().splitlines():
        if line.startswith("ReadWritePaths="):
            paths += [p.lstrip("-") for p in line.split("=", 1)[1].split()]
    return paths


def test_read_write_paths_cover_everything_the_code_writes() -> None:
    from dataclasses import asdict

    from tgpanel.apply.config import ApplyPaths

    rw = _rw_paths()
    writable = [
        v for k, v in asdict(ApplyPaths()).items() if isinstance(v, str) and v.startswith("/")
    ]
    props = ApplyPaths()
    writable += [props.journal, props.nft_file, props.pool_unit, props.pool_env(1)]
    writable += [props.check_profiles, props.check_config, props.legacy_off_dropin]
    for path in writable:
        assert any(path == r or path.startswith(r + "/") for r in rw), path
    # optional paths must carry the '-' prefix so a missing directory cannot break startup
    for line in (DEPLOY / "tgpanel.service").read_text().splitlines():
        if line.startswith("ReadWritePaths="):
            assert all(p.startswith("-") for p in line.split("=", 1)[1].split())
    assert "RuntimeDirectory=tgpanel" in (DEPLOY / "tgpanel.service").read_text()
    assert "/run " not in (DEPLOY / "tgpanel.service").read_text().split("ReadWritePaths=")[1]


def test_restart_script_takes_the_apply_lock() -> None:
    text = (DEPLOY.parent / "scripts" / "restart-pools.sh").read_text()
    assert "flock -w 120" in text and "/var/lib/tgpanel/apply.lock" in text


def test_lock_file_matches_pyproject_runtime_deps() -> None:
    import re
    import tomllib

    root = DEPLOY.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    names = {re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0].lower().replace("_", "-") for d in deps}
    lock = (root / "requirements.lock").read_text()
    locked = {
        m.group(1).lower().replace("_", "-")
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", lock, re.M)
    }
    assert names <= locked, names - locked
    entries = re.split(r"^(?=[A-Za-z0-9_.-]+==)", lock, flags=re.M)[1:]
    assert entries and all("--hash=sha256:" in e for e in entries)
