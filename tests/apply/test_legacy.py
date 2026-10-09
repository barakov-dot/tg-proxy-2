from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.apply.conftest import POOL_UNIT, Env, add_users
from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.importer import Importer

LEGACY_UNIT = "/etc/systemd/system/mtproxy.service"


async def _imported(make: Callable[..., Env]) -> Env:
    env = make("owner")
    imp = Importer(env.pipeline)
    assert (await imp.confirm(await imp.preview())).ok
    env.fake.clear_calls()
    return env


async def test_off_refused_while_profiles_use_the_legacy_port(make: Callable[..., Env]) -> None:
    env = make("owner")
    with pytest.raises(OperationRejected, match="Сначала импортируйте"):
        await Importer(env.pipeline).legacy_mtproxy(False)
    assert env.fake.calls_of("systemctl", "mask") == []


async def test_off_masks_now_and_on_unmasks_and_starts(make: Callable[..., Env]) -> None:
    env = await _imported(make)
    imp = Importer(env.pipeline)
    await imp.legacy_mtproxy(False, "system")
    assert env.fake.systemctl_calls("mask-now") == [("mask-now", "mtproxy")]
    assert "mtproxy" in env.fake.masked and "mtproxy" not in env.fake.active
    await imp.legacy_mtproxy(True, "system")
    assert "mtproxy" not in env.fake.masked and "mtproxy" in env.fake.active
    assert [c[1] for c in env.fake.calls_of("systemctl") if c[2] == "mtproxy.service"] == [
        "mask-now",
        "unmask",
        "start",
    ]
    assert "legacy.off" in env.audit_text() and "legacy.on" in env.audit_text()


async def test_force_allows_off_before_import(make: Callable[..., Env]) -> None:
    env = make("owner")
    await Importer(env.pipeline).legacy_mtproxy(False, "system", force=True)
    assert "mtproxy" in env.fake.masked


async def test_apply_keeps_working_after_the_legacy_unit_is_masked(
    make: Callable[..., Env],
) -> None:
    env = await _imported(make)
    await Importer(env.pipeline).legacy_mtproxy(False)
    # a masked unit is /dev/null: empty content, drop-ins still around
    env.fake.files[LEGACY_UNIT].data = b""
    out = await env.pipeline.run_operation(add_users(env.clock, 1, pool_id=1), reason="x")
    assert out.ok, out.error
    assert any("сохранённой копии" in w for w in out.warnings)
    assert env.fake.files[POOL_UNIT].data  # unit unchanged: facts came from the cache


async def test_legacy_off_without_unit_cache_fails_apply_cleanly(make: Callable[..., Env]) -> None:
    env = make("clean")
    del env.fake.files[LEGACY_UNIT]
    out = await env.pipeline.apply_now("x", force_external=True)
    assert not out.ok and "MTProxy" in (out.error or "")


async def test_mask_failure_is_reported(make: Callable[..., Env]) -> None:
    env = await _imported(make)
    env.fake.fail_on("systemctl", "mask-now")
    with pytest.raises(OperationRejected):
        await Importer(env.pipeline).legacy_mtproxy(False)
