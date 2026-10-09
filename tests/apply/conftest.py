"""Fixtures for pipeline tests: FakeSystemOps + a file-backed SQLite database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.domain.addresses import allocate_addresses
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.domain.secrets_ import generate_secret
from tgpanel.system.fake import FakeSystemOps

T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
SECRET_RE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")
PROFILES = "/etc/tproxy-server/profiles.json"
CONFIG = "/etc/tproxy-server/config.json"
NFT_FILE = "/etc/tgpanel/tgpanel.nft"
POOL_UNIT = "/etc/systemd/system/tgpanel-mtproxy@.service"
RELAY = "tproxy-server"


class Clock:
    """Strictly increasing fake clock: every call advances one second."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


async def no_sleep(_: float) -> None:
    return None


@dataclass
class Env:
    fake: FakeSystemOps
    db: Database
    pipeline: ApplyPipeline
    clock: Clock
    config: ApplyConfig

    def setting(self, key: str) -> str | None:
        return self.db.call(repo.get_setting, key)

    def set_setting(self, key: str, value: str) -> None:
        self.db.call(repo.set_setting, key, value)

    def files(self) -> dict[str, tuple[bytes, int, str, str]]:
        """Snapshot of every fake file outside the backup directory."""
        return {
            p: (f.data, f.mode, f.owner, f.group)
            for p, f in self.fake.files.items()
            if not p.startswith("/var/backups/")
        }

    def tables(self) -> dict[str, list[tuple[Any, ...]]]:
        out: dict[str, list[tuple[Any, ...]]] = {}
        for t in ("users", "pools", "settings", "access_requests", "admins", "counter_state"):
            sql = f"SELECT * FROM {t} ORDER BY 1"  # noqa: S608 - fixed names
            out[t] = [tuple(r) for r in self.db.call(lambda c, q=sql: c.execute(q).fetchall())]
        return out

    def runs(self) -> list[repo.ApplyRun]:
        return self.db.call(repo.list_apply_runs, 100)

    def users(self) -> list[Any]:
        return self.db.call(repo.all_users)

    def audit_text(self) -> str:
        return "\n".join(
            f"{a.actor}|{a.action}|{a.target}|{a.details}"
            for a in self.db.call(repo.list_audit, limit=10_000)
        )

    def all_secrets(self) -> set[str]:
        out: set[str] = set()
        for u in self.users():
            out.add(u.secret)
            out.add(u.mtproxy_secret)
        sent = self.setting("sentinel_secret")
        if sent:
            out.add(sent)
        return out

    def assert_no_secret_leaks(self, *extra: str) -> None:
        """No secret may appear in the call log, audit, apply_runs, backups table or errors."""
        secrets = self.all_secrets()
        haystacks = [
            repr(self.fake.calls),
            self.audit_text(),
            "\n".join(f"{r.error}|{r.reason}|{r.backup_path}" for r in self.runs()),
            *extra,
        ]
        for text in haystacks:
            assert not SECRET_RE.search(text), "secret-looking string leaked"
            for secret in secrets:
                assert secret not in text


def make_env(
    root: Path,
    variant: Literal["clean", "owner"] = "clean",
    *,
    seed: bool = True,
    fake: FakeSystemOps | None = None,
    timing: ApplyTiming | None = None,
) -> Env:
    fake = fake or FakeSystemOps()
    if seed:
        fake.seed_upstream(variant)
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / "tgpanel.db")
    db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    clock = Clock()
    config = ApplyConfig(
        timing=timing
        or ApplyTiming(
            healthz_attempts=3,
            readyz_attempts=2,
            healthz_interval_s=0.0,
            readyz_interval_s=0.0,
            port_timeout_s=0.1,
            lock_timeout_s=1.0,
        )
    )
    pipeline = ApplyPipeline(fake, db, config, clock=clock, sleep=no_sleep)
    return Env(fake, db, pipeline, clock, config)


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Env]]:
    created: list[Env] = []

    def factory(variant: Literal["clean", "owner"] = "clean", **kw: Any) -> Env:
        env = make_env(tmp_path / f"env{len(created)}", variant, **kw)
        created.append(env)
        return env

    yield factory
    for env in created:
        env.pipeline.close()
        env.db.close()


@pytest.fixture
async def env(make: Callable[..., Env]) -> Env:
    """Clean upstream server with the panel baseline applied (sentinel profile, pool 1 up)."""
    e = make("clean")
    drop_foreign(e.fake)
    out = await e.pipeline.apply_now("init", force_external=True)
    assert out.ok, out.error
    e.fake.clear_calls()
    return e


def add_users(
    clock: Callable[[], datetime],
    n: int,
    *,
    pool_id: int = 1,
    status: UserStatus = UserStatus.ACTIVE,
    new_pool: PoolRecord | None = None,
) -> Callable[[sqlite3.Connection], list[int]]:
    """Mutation inserting ``n`` users (optionally creating ``new_pool`` first)."""

    def mutation(conn: sqlite3.Connection) -> list[int]:
        now = clock()
        if new_pool is not None:
            repo.insert_pool(conn, new_pool, now)
        ips = allocate_addresses(repo.used_loopback_ips(conn), n)
        base = conn.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
        ids = []
        for i in range(n):
            ids.append(
                repo.insert_user(
                    conn,
                    name=f"user {base + i + 1}",
                    secret=generate_secret(),
                    status=status,
                    pool_id=pool_id if new_pool is None else new_pool.id,
                    loopback_ip=ips[i],
                    created_at=now,
                )
            )
        return ids

    return mutation


def set_status_mut(ids: list[int], status: UserStatus) -> Callable[[sqlite3.Connection], None]:
    def mutation(conn: sqlite3.Connection) -> None:
        for uid in ids:
            repo.update_user(conn, uid, status=status)

    return mutation


ALLOWED_WRITE_PREFIXES = (
    PROFILES,
    CONFIG,
    "/etc/tgpanel/",
    POOL_UNIT,
    "/var/backups/tgpanel/",
    "/etc/tproxy-server/.tgpanel-check-",
    "/var/lib/tgpanel/apply.journal",
)


def assert_only_allowed_writes(fake: FakeSystemOps) -> None:
    """Nothing but profiles.json, config.json, our own files and backups was ever touched."""
    for call in (
        *fake.calls_of("write_atomic"),
        *fake.calls_of("remove"),
        *fake.calls_of("make_tar_gz"),
    ):
        assert str(call[1]).startswith(ALLOWED_WRITE_PREFIXES), call
    assert b"admin off" in fake.files["/etc/caddy/Caddyfile"].data
    assert b"tgpanel" not in fake.files["/etc/caddy/Caddyfile"].data
    for path in ("/etc/mtproxy/mtproxy.env", "/etc/systemd/system/mtproxy.service"):
        assert not [c for c in fake.calls_of("write_atomic") if c[1] == path]


def drop_foreign(fake: FakeSystemOps) -> None:
    """Replace the legacy "default" profile with a sentinel-like entry (no unmanaged profiles)."""
    fake.files[PROFILES].data = (
        b'{"profiles": [{"name": "_tgpanel_sentinel", "secret": "'
        + b"f" * 32
        + b'", "backend": "127.0.0.1:2400"}]}'
    )
