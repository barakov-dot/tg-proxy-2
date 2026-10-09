"""Fixtures for ops CLI tests: FakeSystemOps + FakeShellTools around a seeded upstream install."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgpanel import ops_cli
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import CertInfo
from tgpanel.system.tools import FakeShellTools

DOMAIN = "panel.example.com"
PANEL_PATH = "Abcdefghijklmnopqrstuv123456"
CADDYFILE = "/etc/caddy/Caddyfile"
PROFILES = "/etc/tproxy-server/profiles.json"
NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
FAST = ApplyConfig(
    timing=ApplyTiming(
        healthz_attempts=2,
        readyz_attempts=1,
        healthz_interval_s=0,
        readyz_interval_s=0,
        port_timeout_s=0.1,
        lock_timeout_s=1,
    )
)


def good_cert(days: int = 80, issued_days_ago: int = 10) -> CertInfo:
    return CertInfo(
        issuer="Let's Encrypt",
        not_before=(NOW - timedelta(days=issued_days_ago)).isoformat(),
        not_after=(NOW + timedelta(days=days)).isoformat(),
        valid_chain=True,
    )


async def no_sleep(_: float) -> None:
    return None


class Ops:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FakeSystemOps()
        self.fake.seed_upstream("clean")
        self.tools = FakeShellTools()
        self.db = str(tmp_path / "ops.db")
        self.answers: list[str] = []
        fake = self.fake
        fake.put_file(
            "/etc/tgpanel/tgpanel.env",
            f"TGPANEL_PANEL_DOMAIN={DOMAIN}\nTGPANEL_PANEL_PATH={PANEL_PATH}\n"
            "TGPANEL_BOT_TOKEN=123456:secret-token-value-secret-token-v\n",
            mode=0o600,
        )
        for rel in (
            "tgpanel.service",
            "tgpanel-mtproxy-refresh.path",
            "tgpanel-mtproxy-refresh.service",
            "tgpanel-cli",
        ):
            fake.put_file(f"/opt/tgpanel/deploy/{rel}", (DEPLOY / rel).read_bytes())
        fake.put_file("/opt/tgpanel/requirements.lock", b"# lock\n")
        fake.put_file("/usr/local/bin/tproxy-server", b"relay-binary")
        fake.put_file("/usr/local/bin/caddy", b"caddy-binary")
        fake.put_file("/opt/MTProxy/objs/bin/mtproto-proxy", b"mtproxy-binary")
        fake.put_file("/etc/os-release", b'ID=ubuntu\nVERSION_ID="24.04"\n')
        fake.put_file("/var/lib/caddy/.local/share/caddy/keep.txt", b"caddy storage")
        fake.mkdir("/var/lib/tgpanel")
        fake.set_dns(DOMAIN, a=[fake.public_ip or ""], aaaa=[])
        fake.set_cert(DOMAIN, good_cert())
        fake.set_port_open(8090)
        for unit in (
            "tgpanel",
            "tgpanel-firewall",
            "tgpanel-mtproxy-refresh.path",
        ):
            fake.set_active(unit)
        self.tools.refs["main"] = "b" * 40
        self.tools.refs["v2"] = "c" * 40

    def __call__(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = ops_cli.main(
            ["--db", self.db, *argv],
            ops=self.fake,
            tools=self.tools,
            config=FAST,
            input_fn=lambda _prompt: self.answers.pop(0),
            out=out,
            sleep=no_sleep,
            clock=lambda: NOW,
        )
        return code, out.getvalue()

    def install(self) -> None:
        """What install.sh does, step by step (without the shell parts)."""
        assert self("pre-install-backup")[0] == 0
        assert self("bootstrap", "--domain", DOMAIN, "--login", "admin", "--admin-id", "42")[0] == 0
        self.fake.files.pop("/etc/systemd/system/tgpanel.service", None)
        assert self("caddy-install", "--domain", DOMAIN, "--path", PANEL_PATH)[0] == 0
        from tgpanel import cli

        buf = io.StringIO()
        assert cli.main(["--db", self.db, "apply"], ops=self.fake, config=FAST, out=buf) == 0, (
            buf.getvalue()
        )


@pytest.fixture
def ops(tmp_path: Path) -> Ops:
    return Ops(tmp_path)


@pytest.fixture
def installed(ops: Ops) -> Ops:
    ops.install()
    return ops


Run = Callable[..., tuple[int, str]]
