# ruff: noqa: RUF001, S311
"""Visual preview of the REAL composed web app with synthetic data. Never touches the system.

    ~/.venvs/tgpanel/bin/python -m tests.tools.preview_web

Opens http://127.0.0.1:8099/preview/ : login ``admin`` / password ``preview-only``. The data is
synthetic (40 users, 30 days of traffic, some users online), the system is a ``FakeSystemOps``
whose ``/proc`` files change over time, and the database is a temporary SQLite file that is
removed on exit. The bot is not started (no token).
"""

from __future__ import annotations

import asyncio
import io
import math
import random
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from argon2 import PasswordHasher

from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.counters import Counter, floor_day, floor_hour, floor_minute
from tgpanel.domain.models import UserStatus
from tgpanel.main import AppEnv, Stack, compose, prepare
from tgpanel.services.api import NewUser
from tgpanel.services.backups import BackupService
from tgpanel.services.traffic import RELAY_HEALTHZ_URL, RELAY_METRICS_URL
from tgpanel.system.fake import FakeFile, FakeSystemOps
from tgpanel.system.ops import CertInfo, DiskUsage

HOST = "127.0.0.1"
PORT = 8099
PANEL_PATH = "preview"
LOGIN = "admin"
PASSWORD = "preview-only"
SECRET_KEY = "preview-secret-key-for-the-local-preview-0123456789"
USERS = 40
ONLINE = 12
FIRST_NAMES = (
    "Анна", "Борис", "Вера", "Глеб", "Дарья", "Егор", "Жанна", "Игорь", "Кира", "Лев",
    "Мария", "Назар", "Ольга", "Павел", "Роман", "Софья", "Тимур", "Ульяна", "Фёдор", "Элла",
)  # fmt: skip


class ProcSim:
    """Plausible, slowly varying /proc/stat, meminfo, net/dev, loadavg, uptime."""

    def __init__(self, now: Callable[[], float] = time.monotonic, seed: int = 7) -> None:
        self._now = now
        self._rng = random.Random(seed)
        self._start = self._last = now()
        self.cores = 4
        self.busy = 4_000_000
        self.total = 40_000_000
        self.rx = 2_000_000_000
        self.tx = 900_000_000

    def _advance(self) -> float:
        now = self._now()
        dt = max(now - self._last, 0.0)
        self._last = now
        t = now - self._start
        load = min(0.95, max(0.03, 0.30 + 0.22 * math.sin(t / 17) + self._rng.uniform(-0.05, 0.05)))
        jiffies = int(dt * 100 * self.cores)
        self.total += jiffies
        self.busy += int(jiffies * load)
        rx_rate = max(0.2, 18 + 14 * math.sin(t / 23) + self._rng.uniform(-3, 3))  # Mbit/s
        tx_rate = max(0.1, 6 + 4 * math.sin(t / 29 + 1) + self._rng.uniform(-1, 1))
        self.rx += int(dt * rx_rate * 1e6 / 8)
        self.tx += int(dt * tx_rate * 1e6 / 8)
        return t

    def read(self, path: str) -> bytes | None:
        t = self._advance()
        if path == "/proc/stat":
            idle = self.total - self.busy
            lines = [f"cpu  {self.busy} 0 0 {idle} 0 0 0 0 0 0"]
            lines += [f"cpu{i} 1 0 0 1 0 0 0 0 0 0" for i in range(self.cores)]
            return ("\n".join(lines) + "\n").encode()
        if path == "/proc/meminfo":
            used = int((3.1 + 0.4 * math.sin(t / 40)) * 2**20)  # kB
            total = 8 * 2**20
            return (
                f"MemTotal: {total} kB\nMemFree: 500000 kB\nMemAvailable: {total - used} kB\n"
                "Buffers: 100000 kB\nCached: 1500000 kB\n"
            ).encode()
        if path == "/proc/loadavg":
            return f"{0.6 + 0.3 * math.sin(t / 30):.2f} 0.55 0.48 2/311 4242\n".encode()
        if path == "/proc/uptime":
            return f"{9 * 86400 + 5 * 3600 + t:.2f} 1234.5\n".encode()
        if path == "/proc/net/dev":
            return (
                "Inter-|   Receive | Transmit\n face |bytes packets errs drop fifo frame comp mcast"
                "|bytes packets errs drop fifo colls carrier comp\n"
                "    lo: 12345 10 0 0 0 0 0 0 12345 10 0 0 0 0 0 0\n"
                f"  eth0: {self.rx} 1000 0 0 0 0 0 0 {self.tx} 900 0 0 0 0 0 0\n"
                "docker0: 99999999 10 0 0 0 0 0 0 99999999 10 0 0 0 0 0 0\n"
            ).encode()
        if path == "/proc/net/route":
            return (
                b"Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                b"eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\n"
            )
        return None


class PreviewOps(FakeSystemOps):
    """FakeSystemOps whose /proc files are alive."""

    def __init__(self, sim: ProcSim) -> None:
        super().__init__()
        self.sim = sim

    async def read_file(self, path: str) -> bytes:
        if path.startswith("/proc/"):
            data = self.sim.read(path)
            if data is not None:
                return data
        return await super().read_file(path)


def synthetic_name(i: int) -> str:
    return f"{FIRST_NAMES[i % len(FIRST_NAMES)]} {chr(ord('А') + (i * 3) % 26)}."


def fill_history(db: Database, user_ids: list[int], now: datetime, rng: random.Random) -> None:
    """30 days of traffic: minutes (last 24 h), hours (last 14 days), days (older)."""

    def work(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            for rank, uid in enumerate(user_ids):
                weight = 0.15 + 1.6 / (1 + rank % 9)  # a few heavy users, a long tail
                top = floor_minute(now)
                for m in range(0, 24 * 60, 5):
                    ts = top - timedelta(minutes=m)
                    base = weight * (1 + 0.8 * math.sin(m / 180)) * rng.uniform(0.2, 1.6)
                    repo.add_traffic(
                        conn, "minute", uid, ts,
                        bytes_up=int(base * 40_000), bytes_down=int(base * 380_000),
                    )  # fmt: skip
                hour_top = floor_hour(now) - timedelta(days=1)
                for h in range(0, 13 * 24):
                    ts = hour_top - timedelta(hours=h)
                    base = weight * (1 + 0.6 * math.sin(h / 4)) * rng.uniform(0.1, 1.5)
                    repo.add_traffic(
                        conn, "hour", uid, ts,
                        bytes_up=int(base * 600_000), bytes_down=int(base * 5_500_000),
                    )  # fmt: skip
                day_top = floor_day(now) - timedelta(days=14)
                for d in range(0, 17):
                    ts = day_top - timedelta(days=d)
                    base = weight * rng.uniform(0.2, 1.4)
                    repo.add_traffic(
                        conn, "day", uid, ts,
                        bytes_up=int(base * 14_000_000), bytes_down=int(base * 130_000_000),
                    )  # fmt: skip

    db.call(work)


def mark_online(db: Database, user_ids: list[int], now: datetime) -> None:
    """Keep ``user_ids`` online (a user is online while its counter state is fresh)."""

    def work(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            for uid in user_ids:
                repo.put_counter_state(
                    conn, repo.CounterStateRow(uid, Counter(0, 0), Counter(0, 0), True, True, now)
                )
                repo.update_user(conn, uid, last_seen_at=now)

    db.call(work)


async def build_preview(root: Path, *, clock_seed: int = 7) -> Stack:
    """The real composed stack (``tgpanel.main.compose``) over fake system + synthetic data."""
    rng = random.Random(clock_seed)
    sim = ProcSim(seed=clock_seed)
    ops = PreviewOps(sim)
    ops.seed_upstream("clean")
    ops.files["/etc/tproxy-server/profiles.json"] = FakeFile(
        b'{"profiles": [{"name": "_tgpanel_sentinel", "secret": "' + b"f" * 32
        + b'", "backend": "127.0.0.1:2400"}]}', 0o400, "root", "tproxy",
    )  # fmt: skip
    ops.disks["/var/lib/tgpanel"] = DiskUsage(200 * 2**30, 31 * 2**30)
    ops.set_http(
        RELAY_METRICS_URL,
        200,
        b"tproxy_sessions_live 137\ntproxy_streams_live 801\ntproxy_limit_hits_total 3\n",
    )
    ops.set_http(RELAY_HEALTHZ_URL, 200, b"ok")
    ops.set_cert(
        "panel.example.com",
        CertInfo("Preview CA", "2026-01-01T00:00:00Z", "2026-12-01T00:00:00Z", True),
    )
    env = AppEnv(
        "", (1,), "panel.example.com", PANEL_PATH, SECRET_KEY, str(root / "preview.db"),
        HOST, PORT, str(root / "tgpanel.env"),
    )  # fmt: skip
    config = ApplyConfig(timing=ApplyTiming(healthz_interval_s=0.0, readyz_interval_s=0.0))
    hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    stack = compose(env, ops, config=config, password_hasher=hasher, extra_hosts=[f"{HOST}:{PORT}"])
    stack.web.cookie_secure = False  # plain http on loopback; browsers keep the cookie
    db = stack.ctx.db
    db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    db.call(repo.set_setting, "panel_login", LOGIN)
    db.call(repo.set_setting, "panel_password_hash", hasher.hash(PASSWORD))
    db.call(repo.set_setting, "panel_session_version", "1")
    await prepare(stack)
    out = await stack.pipeline.apply_now("preview-init", force_external=True)
    if not out.ok:
        raise RuntimeError(out.error)
    await stack.ctx.users.load_hostname()

    users = [NewUser(name=synthetic_name(i), comment="") for i in range(USERS)]
    result = await stack.ctx.users.create(users, "system")
    if not result.ok:
        raise RuntimeError(result.error)
    ids = list(result.user_ids)
    now = stack.pipeline.now()
    for i, uid in enumerate(ids):
        fields: dict[str, object] = {"comment": ["", "vip", "семья", "коллеги", "тест"][i % 5]}
        if i % 3 != 2:
            fields["tg_id"] = 100_000_000 + i * 7919
        if i % 4 == 0:
            fields["tg_username"] = f"user_{i:02d}_tg"
            fields["bot_started"] = True
        if i % 11 == 5:
            fields["status"] = UserStatus.DISABLED
        elif i % 9 == 8:
            fields["status"] = UserStatus.EXPIRED
            fields["expires_at"] = now - timedelta(days=2 + i % 5)
        elif i % 6 == 1:
            fields["expires_at"] = now + timedelta(days=2 + i % 20)
        else:
            fields["expires_at"] = now + timedelta(days=60 + i * 3)
        if i % 5 != 0:
            fields["first_seen_at"] = now - timedelta(days=10 + i)
            fields["last_seen_at"] = now - timedelta(hours=1 + i * 3)
        db.call(repo.update_user, uid, **fields)
    db.call(lambda c: c.execute("UPDATE users SET imported = 1 WHERE id % 10 = 0"))
    db.call(lambda c: c.commit())
    fill_history(db, ids, now, rng)
    online_ids = [uid for i, uid in enumerate(ids) if i % 3 == 0][:ONLINE]
    mark_online(db, online_ids, now)
    stack.preview_online = (online_ids,)  # type: ignore[attr-defined]
    for n in range(3):
        db.call(repo.create_access_request, 700_000 + n, f"guest{n}", f"Гость {n}", now)
    await stack.pipeline.create_backup("preview", "system", full=True)
    stack.web.backups = BackupService(
        stack.pipeline, opener=lambda path: io.BytesIO(ops.files[path].data)
    )
    return stack


async def serve(root: Path) -> None:
    stack = await build_preview(root)
    online_ids = stack.preview_online[0]  # type: ignore[attr-defined]
    config = uvicorn.Config(
        stack.app, host=HOST, port=PORT, access_log=False, log_level="warning", server_header=False
    )
    server = uvicorn.Server(config)

    async def keep_online() -> None:
        while True:
            await asyncio.sleep(20)
            mark_online(stack.ctx.db, online_ids, stack.pipeline.now())

    task = asyncio.create_task(keep_online())
    print(f"Preview (synthetic data, fake system): http://{HOST}:{PORT}/{PANEL_PATH}/", flush=True)
    print(f"Login: {LOGIN}   Password: {PASSWORD}   (Ctrl+C to stop)", flush=True)
    try:
        await server.serve()
    finally:
        task.cancel()
        stack.ctx.close()


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="tgpanel-preview-"))
    try:
        asyncio.run(serve(root))
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
