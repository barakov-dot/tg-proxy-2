from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.collector.poller import Collector
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import SetCounter

T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
SECRET_RE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")


@dataclass
class CEnv:
    fake: FakeSystemOps
    db: Database
    pipeline: ApplyPipeline
    collector: Collector
    secrets: dict[int, str]

    def add_user(
        self,
        n: int,
        *,
        created_at: datetime = T0 - timedelta(days=1),
        status: UserStatus = UserStatus.ACTIVE,
        element: bool = True,
    ) -> str:
        """Insert user n (ip 127.64.0.n); optionally add its nft elements with zero counters."""
        ip = f"127.64.0.{n}"
        secret = f"{n:032x}"
        uid = self.db.call(
            repo.insert_user,
            name=f"user{n}",
            secret=secret,
            status=status,
            pool_id=1,
            loopback_ip=ip,
            created_at=created_at,
        )
        self.secrets[uid] = secret
        if element:
            for name in ("up", "down"):
                self.fake.ensure_set("tgpanel", name).setdefault(ip, SetCounter(0, 0))
        return ip

    def uid(self, ip: str) -> int:
        return int(
            self.db.call(
                lambda c: c.execute("SELECT id FROM users WHERE loopback_ip = ?", (ip,)).fetchone()[
                    0
                ]
            )
        )

    def traffic(self, ip: str, tier: str = "minute") -> list[repo.TrafficPoint]:
        far = datetime(2100, 1, 1, tzinfo=UTC)
        return self.db.call(
            repo.get_traffic, tier, self.uid(ip), datetime(2000, 1, 1, tzinfo=UTC), far
        )

    def state(self, ip: str) -> repo.CounterStateRow | None:
        return self.db.call(repo.get_counter_state, self.uid(ip))

    def user_extra(self, ip: str) -> repo.UserExtra:
        extra = self.db.call(repo.get_user_extra, self.uid(ip))
        assert extra is not None
        return extra


@pytest.fixture
async def cenv(tmp_path: Path) -> AsyncIterator[CEnv]:
    fake = FakeSystemOps()
    fake.ensure_set("tgpanel", "up")
    fake.ensure_set("tgpanel", "down")
    db = Database(tmp_path / "c.db")
    db.call(repo.insert_pool, PoolRecord(id=1, port=2400, stats_port=8900), T0)

    async def no_sleep(_: float) -> None:
        return None

    pipeline = ApplyPipeline(
        fake, db, ApplyConfig(timing=ApplyTiming()), clock=lambda: T0, sleep=no_sleep
    )
    collector = Collector(fake, pipeline, clock=lambda: T0, write_timeout_s=0.05)
    env = CEnv(fake, db, pipeline, collector, {})
    yield env
    # global guards: never /readyz, only nft + (optionally) /metrics, /healthz
    for call in fake.calls:
        assert call[0] in ("nft_list_set", "http_get"), call
        if call[0] == "http_get":
            assert "readyz" not in call[1]
    pipeline.close()
    db.close()


Poll = Callable[[int], datetime]


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)
