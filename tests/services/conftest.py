from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.apply.conftest import SECRET_RE, Clock, no_sleep
from tgpanel.apply.config import ApplyConfig, ApplyTiming
from tgpanel.db import repo
from tgpanel.services.container import AppContext, build_context
from tgpanel.services.users import UserServiceImpl
from tgpanel.system.fake import FakeSystemOps

PROFILES = "/etc/tproxy-server/profiles.json"
RELAY = "tproxy-server"


@dataclass
class Svc:
    ctx: AppContext
    fake: FakeSystemOps
    clock: Clock

    @property
    def users(self) -> UserServiceImpl:
        return self.ctx.users

    def runs(self) -> list[repo.ApplyRun]:
        return self.ctx.db.call(repo.list_apply_runs, 100)

    def secrets(self) -> set[str]:
        out: set[str] = set()
        for u in self.ctx.db.call(repo.all_users):
            out |= {u.secret, u.mtproxy_secret}
        sentinel = self.ctx.db.call(repo.get_setting, "sentinel_secret")
        if sentinel:
            out.add(sentinel)
        return out

    def profile_names(self) -> list[str]:
        return [p["name"] for p in self.fake.get_json(PROFILES)["profiles"]]

    def audit_text(self) -> str:
        return "\n".join(
            f"{a.actor}|{a.action}|{a.target}|{a.details}"
            for a in self.ctx.db.call(repo.list_audit, limit=10_000)
        )

    def assert_clean(self, *texts: str) -> None:
        """No secret anywhere a log/audit/error could carry one."""
        haystacks = [
            repr(self.fake.calls),
            self.audit_text(),
            "\n".join(f"{r.error}|{r.reason}" for r in self.runs()),
            *texts,
        ]
        for text in haystacks:
            assert not SECRET_RE.search(text)
            for secret in self.secrets():
                assert secret not in text


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Svc]:
    fake = FakeSystemOps()
    fake.seed_upstream("clean")
    clock = Clock()
    config = ApplyConfig(
        timing=ApplyTiming(
            healthz_attempts=3,
            readyz_attempts=2,
            healthz_interval_s=0.0,
            readyz_interval_s=0.0,
            port_timeout_s=0.1,
            lock_timeout_s=1.0,
        )
    )
    ctx = build_context(fake, tmp_path / "svc.db", config=config, clock=clock, sleep=no_sleep)
    ctx.db.call(repo.set_setting, "proxy_hostname", "proxy.example.com")
    out = await ctx.pipeline.apply_now("init", force_external=True)
    assert out.ok, out.error
    fake.clear_calls()
    yield Svc(ctx, fake, clock)
    ctx.close()
