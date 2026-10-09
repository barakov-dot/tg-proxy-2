"""Every PLAN 8.3 command parses and dispatches through the single entry point ``tgpanel.cli``."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.ops.conftest import DOMAIN, FAST, NOW, PANEL_PATH, Ops
from tgpanel import cli

PLAN_83 = (
    "status",
    "doctor",
    "apply",
    "import",
    "legacy-mtproxy",
    "backup",
    "restore",
    "show-url",
    "reset-password",
    "repair",
    "update",
    "uninstall",
)
INTERNAL = ("bootstrap", "caddy-install", "pre-install-backup", "migrate")


@pytest.fixture
def run(tmp_path: Path) -> Callable[..., tuple[int, str]]:
    ops = Ops(tmp_path)
    ops.install()
    ops.answers = []

    def call(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(
            ["--db", ops.db, *argv],
            ops=ops.fake,
            tools=ops.tools,
            config=FAST,
            input_fn=lambda _prompt: ops.answers.pop(0),
            out=out,
            sleep=ops.time.sleep,
            clock=lambda: NOW,
            monotonic=ops.time.monotonic,
        )
        return code, out.getvalue()

    return call


def test_every_documented_command_is_registered() -> None:
    parser = cli.build_parser(lambda: None)  # type: ignore[arg-type,return-value]
    sub = next(a for a in parser._actions if a.dest == "command")
    names = set(sub.choices or ())
    assert set(PLAN_83) <= names and set(INTERNAL) <= names


def test_no_command_is_a_usage_error(run: Callable[..., tuple[int, str]]) -> None:
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2


def test_status_apply_backup_import_legacy_dispatch(run: Callable[..., tuple[int, str]]) -> None:
    code, out = run("status")
    assert code == 0 and "Состояние tgpanel" in out
    code, out = run("apply")
    assert code == 0 and "Изменений нет" in out
    code, out = run("backup", "--reason", "wiring")
    assert code == 0 and "Резервная копия создана" in out
    path = re.search(r": (\S+) \(", out)
    assert path
    code, out = run("backup", "--list")
    assert code == 0 and "wiring" in out
    code, out = run("import", "--dry-run")
    assert code == 0
    code, out = run("legacy-mtproxy", "off")
    assert code in (0, 1) and out.strip()
    code, out = run("restore", path.group(1), "--yes")
    assert code == 0 and "восстановлено" in out.lower()


def test_ops_commands_dispatch(run: Callable[..., tuple[int, str]]) -> None:
    code, out = run("show-url")
    assert code == 0 and f"https://{DOMAIN}/{PANEL_PATH}/" in out
    code, out = run("reset-password", "--login", "root")
    assert code == 0 and "Новый пароль" in out and "root" in out
    code, out = run("doctor")
    assert code in (0, 1) and "Диагностика tgpanel" in out
    code, out = run("repair")
    assert code in (0, 1) and "Восстановление компонентов" in out
    code, out = run("update", "--ref", "main")
    assert code in (0, 1, 3) and out.strip()
    code, out = run("migrate")
    assert code == 0 and out.startswith("schema version")
    code, out = run("pre-install-backup")
    assert code == 0 and out.strip().endswith(".tar.gz")


def test_internal_commands_dispatch(run: Callable[..., tuple[int, str]]) -> None:
    code, out = run("bootstrap", f"--domain={DOMAIN}", "--login=admin", "--admin-id=7")
    assert code == 0 and out.strip() in ("credentials-created", "credentials-kept")
    code, out = run("caddy-install", f"--domain={DOMAIN}", f"--path={PANEL_PATH}")
    assert code == 0 and "Caddy" in out


def test_uninstall_dispatches_last(run: Callable[..., tuple[int, str]]) -> None:
    code, out = run("uninstall", "--yes")
    assert code in (0, 1) and out.strip()
