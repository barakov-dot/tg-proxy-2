from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from tests.db.helpers import NOW, fresh_db
from tgpanel.db.connection import open_database, transaction
from tgpanel.db.repo import add_audit, list_audit


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_db_files_and_dir_are_private(tmp_path: Path) -> None:
    db = tmp_path / "data" / "panel.db"
    conn = open_database(db)
    conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    assert mode(db.parent) == 0o700
    assert mode(db) == 0o600
    for suffix in ("-wal", "-shm"):
        p = Path(f"{db}{suffix}")
        assert p.exists() and mode(p) == 0o600
    conn.close()


def test_existing_directory_is_not_chmodded(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    conn = open_database(tmp_path / "x.db")
    assert mode(tmp_path) == 0o755
    conn.close()


def test_rollback_failure_does_not_mask_original_error() -> None:
    class Boom(Exception):
        pass

    class Conn:
        in_transaction = False

        def execute(self, sql: str) -> None:
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("cannot rollback")

    with pytest.raises(Boom), transaction(Conn()):  # type: ignore[arg-type]
        raise Boom


def test_audit_redacts_secret_like_strings() -> None:
    conn = fresh_db()
    sec = "ab" * 16
    add_audit(conn, NOW, "system", "x", target=f"dd{sec.upper()}", details=f"a {sec} b dd{sec}")
    entry = list_audit(conn)[0]
    assert sec not in entry.details and sec.upper() not in entry.target
    assert entry.details == "a [redacted] b [redacted]"
    assert entry.target == "[redacted]"
