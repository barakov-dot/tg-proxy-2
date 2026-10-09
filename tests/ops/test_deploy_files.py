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
