from __future__ import annotations

import json

import pytest

from tests.services.conftest import RELAY, Svc
from tgpanel.apply.settings_spec import SPECS
from tgpanel.db import repo
from tgpanel.domain.models import CarrierMode
from tgpanel.services.api import NewUser

CONFIG = "/etc/tproxy-server/config.json"


async def test_defaults_are_typed(svc: Svc) -> None:
    cfg = await svc.ctx.settings.all()
    assert cfg["secrets_per_process"] == 16
    assert cfg["max_sessions_global"] == 1024 and cfg["max_streams_global"] == 16384
    assert cfg["mtp_max_connections"] == 4096
    assert cfg["carrier_mode_default"] is CarrierMode.HTTPS
    assert cfg["default_term"] == "1m" and cfg["timezone"] == "UTC"
    assert cfg["backup_keep_last"] == 50 and cfg["backup_keep_days"] == 30
    assert cfg["issuance_mode"] == "approval" and cfg["proxy_hostname"] == "proxy.example.com"
    assert set(cfg) == set(SPECS)
    assert await svc.ctx.settings.get("secrets_per_process") == 16


async def test_proxy_setting_applies_and_rewrites_limits(svc: Svc) -> None:
    runs, relay = len(svc.runs()), svc.fake.restart_count(RELAY)
    res = await svc.ctx.settings.set("max_sessions_global", 2048, "web:admin")
    assert res.ok and res.apply_run_id
    limits = json.loads(svc.fake.files[CONFIG].data)["limits"]
    assert limits["max_sessions_global"] == limits["new_sessions_burst"] == 2048
    assert limits["max_bootstraps_global"] == limits["new_bootstraps_burst"] == 2048
    assert len(svc.runs()) == runs + 1 and svc.fake.restart_count(RELAY) == relay + 1
    assert await svc.ctx.settings.get("max_sessions_global") == 2048
    assert "settings.set" in svc.audit_text()


async def test_non_proxy_setting_is_db_only(svc: Svc) -> None:
    runs = len(svc.runs())
    res = await svc.ctx.settings.set_many(
        {"timezone": "Europe/Moscow", "backup_keep_last": 10, "issuance_mode": "open"}, "web:admin"
    )
    assert res.ok and res.apply_run_id is None
    assert svc.fake.calls == [] and len(svc.runs()) == runs
    assert await svc.ctx.settings.get("timezone") == "Europe/Moscow"
    assert await svc.ctx.settings.get("issuance_mode") == "open"


async def test_same_value_changes_nothing(svc: Svc) -> None:
    runs = len(svc.runs())
    assert (await svc.ctx.settings.set("max_sessions_global", 1024, "x")).ok
    assert len(svc.runs()) == runs and svc.fake.calls_of("systemctl") == []


async def test_mixed_keys_are_one_apply(svc: Svc) -> None:
    runs = len(svc.runs())
    res = await svc.ctx.settings.set_many(
        {"max_sessions_global": 512, "max_streams_global": 20000, "timezone": "UTC"}, "x"
    )
    assert res.ok and len(svc.runs()) == runs + 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("secrets_per_process", 0),
        ("secrets_per_process", 17),
        ("secrets_per_process", "abc"),
        ("default_term", "forever"),
        ("default_term_date", "10.10.2026"),
        ("carrier_mode_default", "carrier-pigeon"),
        ("max_sessions_global", 1),
        ("max_sessions_global", 10**9),
        ("max_streams_global", 0),
        ("mtp_max_connections", -5),
        ("mtp_workers", 0),
        ("timezone", "Mars/Olympus"),
        ("backup_keep_last", 0),
        ("backup_keep_days", -1),
        ("proxy_hostname", "not a host"),
        ("proxy_hostname", "-bad.example.com"),
        ("panel_hostname", "a b.example.com"),
        ("issuance_mode", "anarchy"),
        ("nonexistent", "1"),
    ],
)
async def test_validation_rejects(svc: Svc, key: str, value: object) -> None:
    before = svc.ctx.db.call(repo.all_settings)
    runs = len(svc.runs())
    res = await svc.ctx.settings.set(key, value, "x")
    assert not res.ok and res.error
    assert svc.ctx.db.call(repo.all_settings) == before and len(svc.runs()) == runs


async def test_hostnames_are_normalised(svc: Svc) -> None:
    assert (await svc.ctx.settings.set("panel_hostname", "Panel.Example.COM", "x")).ok
    assert await svc.ctx.settings.get("panel_hostname") == "panel.example.com"
    assert (await svc.ctx.settings.set("panel_hostname", "", "x")).ok


async def test_secrets_per_process_cannot_drop_below_occupancy(svc: Svc) -> None:
    res = await svc.users.create([NewUser(name=f"n{i}") for i in range(16)], "x")
    assert res.ok
    low = await svc.ctx.settings.set("secrets_per_process", 15, "x")
    assert not low.ok and "16" in (low.error or "")
    assert await svc.ctx.settings.get("secrets_per_process") == 16


async def test_spp_15_makes_next_user_open_a_second_pool(svc: Svc) -> None:
    assert (await svc.ctx.settings.set("secrets_per_process", 15, "x")).ok
    assert (await svc.users.create([NewUser(name=f"n{i}") for i in range(16)], "x")).ok
    pools = svc.ctx.db.call(repo.list_pools)
    assert len(pools) == 2
    assert svc.fake.get_text("/etc/tgpanel/mtproxy/1.env").count("-S ") == 15


async def test_failed_apply_reverts_the_setting(svc: Svc) -> None:
    before = svc.fake.files[CONFIG].data
    svc.fake.fail_on("systemctl", f"restart {RELAY}")
    res = await svc.ctx.settings.set("max_sessions_global", 4096, "x")
    assert not res.ok and "изменения отменены" in (res.error or "")
    assert await svc.ctx.settings.get("max_sessions_global") == 1024
    assert svc.fake.files[CONFIG].data == before


async def test_budget_rejected_by_relay_check_is_not_applied(svc: Svc) -> None:
    svc.fake.fail_check("tproxy_check", "max_pending_global: budget exceeded")
    res = await svc.ctx.settings.set("max_sessions_global", 100000, "x")
    assert not res.ok and "памяти" in (res.error or "")
    assert await svc.ctx.settings.get("max_sessions_global") == 1024


async def test_default_carrier_mode_change(svc: Svc) -> None:
    await svc.users.create([NewUser(name="a")], "x")
    assert (await svc.ctx.settings.set("carrier_mode_default", "websocket", "x")).ok
    assert (
        svc.fake.get_json("/etc/tproxy-server/profiles.json")["profiles"][0]["carrier_mode"]
        == "websocket"
    )
