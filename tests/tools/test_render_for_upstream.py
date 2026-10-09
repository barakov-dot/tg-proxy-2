# ruff: noqa: S603, S607
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.tools.render_for_upstream import main


def test_generator_renders_all_scenarios(tmp_path: Path) -> None:
    assert main(["x", str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["big-1024", "big-128", "big-4096", "clean", "owner"]
    profiles = json.loads((tmp_path / "big-4096" / "profiles.json").read_text())["profiles"]
    assert len(profiles) == 300
    config = json.loads((tmp_path / "big-4096" / "config.json").read_text())
    assert config["limits"]["max_sessions_global"] == 4096
    for d in tmp_path.iterdir():
        for f in ("config.json", "profiles.json", "Caddyfile", "tgpanel.nft", "site/index.html"):
            assert (d / f).exists()
        assert "# >>> tgpanel" in (d / "Caddyfile").read_text()


@pytest.mark.skipif(shutil.which("tproxy-server") is None, reason="upstream relay not installed")
def test_upstream_check_accepts_everything(tmp_path: Path) -> None:
    main(["x", str(tmp_path)])
    for d in tmp_path.iterdir():
        res = subprocess.run(
            [
                "tproxy-server",
                "-config",
                str(d / "config.json"),
                "-profiles-file",
                str(d / "profiles.json"),
                "-check",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, (d.name, res.stdout, res.stderr)
