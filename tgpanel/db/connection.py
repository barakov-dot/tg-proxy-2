"""SQLite connection helper, migration runner, snapshot and async wrapper.

Single-writer assumption: one process owns the database. A ``Database`` shares one
connection between threads guarded by a lock, so callers may use ``asyncio.to_thread``
(or ``Database.run``) freely.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from tgpanel.db.migrations import MIGRATIONS


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection: WAL, foreign keys, Row factory, autocommit (explicit transactions)."""
    on_disk = str(path) not in ("", ":memory:") and not str(path).startswith("file:")
    old_umask = os.umask(0o077)  # new db/-wal/-shm files are born 0600
    try:
        if on_disk:
            parent = Path(path).parent
            if not parent.exists():
                parent.mkdir(mode=0o700, parents=True)
                os.chmod(parent, 0o700)
        conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
    finally:
        os.umask(old_umask)
    if on_disk:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(f"{path}{suffix}", 0o600)
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK on error. Nested use joins the outer one."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # A failing ROLLBACK must not mask the original exception.
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each atomically. Returns the resulting version."""
    version = current_version(conn)
    for number, script in MIGRATIONS:
        if number <= version:
            continue
        stmt = f"INSERT INTO schema_version (version) VALUES ({int(number)});"  # noqa: S608
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\n{stmt}\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        version = number
    return version


def open_database(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def snapshot_to(conn: sqlite3.Connection, path: str | Path) -> None:
    """Consistent copy of the database via the sqlite3 backup API (file mode 0600)."""
    target = Path(path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(target, 0o600)


class Database:
    """Thread-safe holder of the single connection with an async runner."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = open_database(path)
        self._lock = threading.RLock()

    def call[T](self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run ``fn(conn, *args, **kwargs)`` synchronously under the lock."""
        with self._lock:
            return fn(self.conn, *args, **kwargs)

    async def run[T](self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run ``fn(conn, *args, **kwargs)`` in a worker thread under the lock."""
        return await asyncio.to_thread(self.call, fn, *args, **kwargs)

    def snapshot_to(self, path: str | Path) -> None:
        with self._lock:
            snapshot_to(self.conn, path)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
