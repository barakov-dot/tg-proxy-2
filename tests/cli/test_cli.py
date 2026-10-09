# ruff: noqa: RUF001
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.apply.conftest import SECRET_RE
from tgpanel import cli
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.system.fake import FakeSystemOps

PROFILES = "/etc/tproxy-server/profiles.json"
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


class Run:
    def __init__(self, tmp_path: Path, variant: str = "clean") -> None:
        self.fake = FakeSystemOps()
        self.fake.seed_upstream(variant)  # type: ignore[arg-type]
        self.db = str(tmp_path / "cli.db")
        self.answers: list[str] = []

    def __call__(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(
            ["--db", self.db, *argv],
            ops=self.fake,
            config=FAST,
            input_fn=lambda _prompt: self.answers.pop(0),
            out=out,
        )
        return code, out.getvalue()


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


@pytest.fixture
def owner(tmp_path: Path) -> Run:
    return Run(tmp_path, "owner")


def test_status_fresh_server_reports_foreign_profiles(run: Run) -> None:
    code, out = run("status")
    assert code == 0
    assert "Состояние tgpanel" in out and "tproxy-server: работает" in out
    assert "ИЗМЕНЁН ВНЕ ПАНЕЛИ" in out and "default" in out
    assert "Применений ещё не было" in out
    assert not SECRET_RE.search(out)


def test_status_unhealthy_relay_exit_code(run: Run) -> None:
    run.fake.set_http("/healthz", 503)
    code, out = run("status")
    assert code == 1 and "нет ответа" in out


def test_apply_refuses_foreign_profiles_then_force(run: Run) -> None:
    before = run.fake.files[PROFILES].data
    code, out = run("apply")
    assert code == 3 and "--force-external" in out and "tgpanel import" in out
    assert run.fake.files[PROFILES].data == before
    code, out = run("apply", "--force-external")
    assert code == 0 and "Готово" in out
    code, out = run("apply")
    assert code == 0 and "Изменений нет" in out
    code, out = run("apply", "--reload-nft")
    assert code == 0
    assert run.fake.calls_of("nft_load_file")
    code, out = run("status")
    assert "Последнее применение" in out and "success" in out and "изменений вне панели нет" in out


def test_apply_failure_exit_code_and_message(run: Run) -> None:
    run.fake.fail_on("systemctl", "restart tproxy-server")
    code, out = run("apply", "--force-external")
    assert code == 1 and "изменения отменены" in out
    code, out = run("status")
    assert "failed" in out


def test_import_dry_run_changes_nothing(owner: Run) -> None:
    before = dict(owner.fake.files)
    code, out = owner("import", "--dry-run")
    assert code == 0
    assert "К импорту: 15 из 15" in out and "user_93455874 → 93455874" in out
    assert "Прежний бот должен быть остановлен" in out
    assert owner.fake.calls_of("systemctl") == [] and owner.fake.files.keys() == before.keys()
    assert not SECRET_RE.search(out)


def test_import_confirm_flow(owner: Run) -> None:
    owner.answers = ["n"]
    code, out = owner("import")
    assert code == 1 and "Отменено" in out
    assert json.loads(owner.fake.files[PROFILES].data)["profiles"][0]["name"] == "user_93455874"
    owner.answers = ["y"]
    code, out = owner("import")
    assert code == 0 and "Импортировано пользователей: 15" in out
    assert "legacy-mtproxy off" in out
    names = [p["name"] for p in json.loads(owner.fake.files[PROFILES].data)["profiles"]]
    assert names[0] == "u1" and len(names) == 15
    code, out = owner("import", "--yes")
    assert code == 0 and "Импортировать нечего" in out


def test_import_yes_and_csv(owner: Run, tmp_path: Path) -> None:
    csv = tmp_path / "ids.csv"
    csv.write_text("user_12345;555;Вася;vip\n", encoding="utf-8")
    code, out = owner("import", "--yes", "--csv", str(csv))
    assert code == 0
    code, out = owner("status")
    assert "всего 15" in out


def test_import_bad_regex_and_missing_csv(owner: Run, tmp_path: Path) -> None:
    code, out = owner("import", "--dry-run", "--id-regex", "(")
    assert code == 1 and "ОШИБКА" in out
    code, out = owner("import", "--dry-run", "--csv", str(tmp_path / "nope.csv"))
    assert code == 2 and "CSV" in out


def test_import_failure_reports_and_rolls_back(owner: Run) -> None:
    owner.fake.fail_on("systemctl", "restart tproxy-server")
    code, out = owner("import", "--yes")
    assert code == 1 and "Импорт не выполнен" in out
    assert owner.fake.files[PROFILES].data.startswith(
        b'{\n  "profiles": [\n    {\n      "name": "user_'
    )


def test_backup_list_and_restore(owner: Run) -> None:
    assert owner("import", "--yes")[0] == 0
    code, out = owner("backup", "--reason", "my check")
    assert code == 0 and "my-check" in out
    code, out = owner("backup", "--list")
    assert code == 0 and out.count(".tar.gz") >= 2
    path = next(w for w in out.split() if w.endswith("-my-check.tar.gz"))
    owner.answers = ["n"]
    code, out = owner("restore", path)
    assert code == 1 and "Отменено" in out
    code, out = owner("restore", path, "--yes")
    assert code == 0 and "восстановлено" in out
    code, out = owner("restore", "/var/backups/tgpanel/missing.tar.gz", "--yes")
    assert code == 1 and "не выполнено" in out


def test_legacy_mtproxy(owner: Run) -> None:
    code, out = owner("legacy-mtproxy", "off")
    assert code == 1 and "импорт" in out
    assert owner("import", "--yes")[0] == 0
    code, out = owner("legacy-mtproxy", "off")
    assert code == 0 and "остановлен" in out and "mtproxy" in owner.fake.masked
    code, out = owner("legacy-mtproxy", "on")
    assert code == 0 and "включён" in out and "mtproxy" in owner.fake.active
    code, out = owner("legacy-mtproxy", "off", "--force")
    assert code == 0


def test_db_path_from_environment(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "env.db"
    monkeypatch.setenv("TGPANEL_DB", str(target))
    out = io.StringIO()
    code = cli.main(["status"], ops=run.fake, config=FAST, out=out)
    assert code == 0 and target.exists()
    assert cli.resolve_db_path(None) == str(target)
    assert cli.resolve_db_path("/x.db") == "/x.db"
    monkeypatch.delenv("TGPANEL_DB")
    assert cli.resolve_db_path(None) == "/var/lib/tgpanel/tgpanel.db"


def test_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGPANEL_LOCK", "/srv/fixture/x.lock")
    monkeypatch.setenv("TGPANEL_BACKUP_DIR", "/srv/fixture/bk")
    cfg = cli.config_from_env()
    assert cfg.paths.lock == "/srv/fixture/x.lock" and cfg.paths.backups_dir == "/srv/fixture/bk"


def test_unknown_command_is_a_usage_error(run: Run) -> None:
    with pytest.raises(SystemExit) as exc:
        run("frobnicate")
    assert exc.value.code == 2
