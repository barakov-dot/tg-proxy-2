# ruff: noqa: S603, S607, ASYNC221, E501, RUF028, RUF100
"""`tgpanel update` with REAL git and a REAL interpreter: stage 2 runs the NEW code.

A temp "origin" holds two commits of a copy of this project. Commit 2 adds a DB migration
(schema 2), a new unit and drops an old one. Stage 2 is a separate `python -P` process whose
PYTHONPATH is the checked-out tree; it uses a FakeSystemOps (nothing touches the machine) and
reports what it did into a JSON file.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tgpanel import ops_cli
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.system import tools as tools_mod
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.tools import RealShellTools

ROOT = Path(__file__).resolve().parents[2]
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

HARNESS = r"""
import json, os, sys
from tgpanel import ops_cli
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.system.fake import FakeSystemOps

repo, db, out = os.environ["REPO"], os.environ["DB"], os.environ["OUT"]
fake = FakeSystemOps()
fake.seed_upstream("clean")
for name in os.listdir(repo + "/deploy"):
    fake.put_file(repo + "/deploy/" + name, open(repo + "/deploy/" + name, "rb").read())
fake.put_file("/etc/systemd/system/tgpanel-legacy-thing.service", b"old unit")
fake.put_file("/etc/systemd/system/tgpanel.service", b"old panel unit")
fake.set_port_open(8090)
cfg = ApplyConfig(timing=ApplyTiming(healthz_attempts=2, readyz_attempts=1, healthz_interval_s=0,
                                     readyz_interval_s=0, port_timeout_s=0.1, lock_timeout_s=1))
code = ops_cli.main(["--db", db, "post-update", "--from=" + os.environ["OLD"], "--to=" + os.environ["NEW"]],
                    ops=fake, config=cfg, install_dir=repo)
json.dump({"code": code, "tgpanel_code": ops_cli.__file__,
           "units": sorted(p.rsplit("/", 1)[-1] for p in fake.files if p.startswith("/etc/systemd/system/tgpanel")),
           "restarted": fake.restart_count("tgpanel")}, open(out, "w"))
sys.exit(1 if os.environ.get("TEST_FAIL") == "1" else code)
"""

MIGRATION_2 = """SQL = "CREATE TABLE added_in_v2 (x INTEGER);"\n"""


def git(*args: str, cwd: Path) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    res = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(cwd)},
    )
    return res.stdout.strip()


def copy_project(dest: Path) -> None:
    for name in ("tgpanel", "deploy"):
        shutil.copytree(
            ROOT / name,
            dest / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"),
        )
    (dest / "tests").mkdir()
    shutil.copytree(ROOT / "tests" / "fixtures", dest / "tests" / "fixtures")
    (dest / "requirements.lock").write_text("# lock\n")
    (dest / "deploy" / "tgpanel-legacy-thing.service").write_text(
        "[Service]\nExecStart=/bin/true\n"
    )


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.work = tmp / "work"
        self.work.mkdir()
        git("init", "-q", "-b", "main", cwd=self.work)
        copy_project(self.work)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "v1", cwd=self.work)
        self.v1 = git("rev-parse", "HEAD", cwd=self.work)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.work), str(tmp / "origin.git")], check=True
        )  # noqa: S603,S607
        subprocess.run(
            ["git", "clone", "-q", str(tmp / "origin.git"), str(tmp / "install")], check=True
        )  # noqa: S603,S607
        self.install = tmp / "install"
        git("checkout", "-q", "--detach", self.v1, cwd=self.install)
        # commit 2: migration, new unit, old unit dropped
        mig = self.work / "tgpanel" / "db" / "migrations"
        (mig / "m0002_test.py").write_text(MIGRATION_2)
        init = (mig / "__init__.py").read_text()
        (mig / "__init__.py").write_text(
            init.replace(
                "MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _M1),)",
                "from tgpanel.db.migrations.m0002_test import SQL as _M2\n\n"
                "MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _M1), (2, _M2))",
            )
        )
        (self.work / "deploy" / "tgpanel-extra.service").write_text(
            "[Service]\nExecStart=/bin/true\n"
        )
        (self.work / "deploy" / "tgpanel-legacy-thing.service").unlink()
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "v2", cwd=self.work)
        self.v2 = git("rev-parse", "HEAD", cwd=self.work)
        git("push", "-q", str(tmp / "origin.git"), "main", cwd=self.work)
        # parent side (OLD code, this process): fake system, real DB file
        self.db = str(tmp / "state" / "tgpanel.db")
        (tmp / "state").mkdir()
        self.fake = FakeSystemOps()
        self.fake.seed_upstream("clean")
        for name in (self.install / "deploy").iterdir():
            self.fake.put_file(f"{self.install}/deploy/{name.name}", name.read_bytes())
        self.fake.put_file(f"{self.install}/requirements.lock", b"# lock\n")
        self.fake.set_active("tgpanel")
        self.fake.set_port_open(8090)
        self.fake.mkdir("/var/lib/tgpanel")
        self.out = tmp / "child.json"
        self.fail_child = False
        self.pip_calls: list[str] = []
        env = self

        class Tools(RealShellTools):
            async def pip_install_locked(self, python: str, lock_file: str) -> None:
                env.pip_calls.append(lock_file)

            async def run_post_update(
                self, python: str, repo: str, db_path: str, old: str, new: str
            ) -> tuple[int, str]:
                import os

                run_env = {
                    **os.environ,
                    "PYTHONPATH": repo,
                    "REPO": repo,
                    "DB": db_path,
                    "OUT": str(env.out),
                    "OLD": old,
                    "NEW": new,
                    "TEST_FAIL": "1" if env.fail_child else "0",
                }
                res = subprocess.run(  # noqa: S603
                    [sys.executable, "-P", "-c", HARNESS],
                    cwd=repo,
                    env=run_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                if res.returncode == 0:
                    env.fake.set_active("tgpanel")  # what the child's restart does for real
                return res.returncode, res.stdout + res.stderr

        self.tools = Tools()

    def run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = ops_cli.main(
            ["--db", self.db, *argv],
            ops=self.fake,
            tools=self.tools,
            config=FAST,
            out=out,
            install_dir=str(self.install),
            input_fn=lambda _p: "y",
        )
        return code, out.getvalue()

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.install)

    def schema(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
        finally:
            conn.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(tools_mod, "_GIT_ENV", {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"})
    e = Env(tmp_path)
    assert e.run("migrate")[0] == 0
    conn = sqlite3.connect(e.db)
    conn.execute("INSERT INTO settings (key, value) VALUES ('marker', 'kept')")
    conn.commit()
    conn.close()
    return e


def test_stage2_runs_new_code_in_a_new_process(env: Env) -> None:
    assert env.schema() == 1
    code, out = env.run("update", "--ref", "main")
    assert code == 0, out
    assert env.head() == env.v2
    assert env.schema() == 2  # the NEW migration ran: only new code knows m0002
    report = json.loads(env.out.read_text())
    assert report["code"] == 0
    assert str(env.install) in report["tgpanel_code"]  # imported from the checked-out tree
    assert "tgpanel-extra.service" in report["units"]
    assert "tgpanel-legacy-thing.service" not in report["units"]  # obsolete unit of ours removed
    assert report["restarted"] == 1
    assert env.v2 in out
    conn = sqlite3.connect(env.db)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0] == "kept"
    finally:
        conn.close()


def test_failed_stage2_rolls_back_code_and_database(env: Env) -> None:
    env.fail_child = True
    code, out = env.run("update", "--ref", "main")
    assert code == 1, out
    assert env.head() == env.v1
    assert env.schema() == 1  # snapshot restored: the schema went back
    conn = sqlite3.connect(env.db)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0] == "kept"
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='added_in_v2'").fetchone()
            is None
        )
    finally:
        conn.close()
    assert "База данных возвращена" in out
    assert any(p.endswith("-pre-update.db") for p in env.fake.files)
    assert len(env.pip_calls) == 2  # new lock, then the old one again


def test_database_newer_than_code_is_refused_by_new_code(env: Env) -> None:
    conn = sqlite3.connect(env.db)
    conn.execute("INSERT INTO schema_version (version) VALUES (99)")
    conn.commit()
    conn.close()
    out = io.StringIO()
    # the OLD code (this process) must refuse to start on such a database as well
    code = ops_cli.main(
        ["--db", env.db, "doctor"], ops=env.fake, tools=env.tools, config=FAST, out=out
    )
    assert code == 2 and "более новой версией" in out.getvalue()
    # ... and so must the stage-2 command of the new code
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-P",
            "-m",
            "tgpanel.ops_cli",
            f"--db={env.db}",
            "post-update",
            f"--from={env.v1}",
            f"--to={env.v2}",
        ],  # fmt: skip
        cwd=env.work,
        env={"PYTHONPATH": str(env.work), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert child.returncode == 2 and "более новой версией" in child.stdout
