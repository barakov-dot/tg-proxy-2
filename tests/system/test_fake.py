from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import CertInfo, HttpResult, SetCounter, SystemOpsError

CFG = "/work/c.json"
PRF = "/work/p.json"
SECRET = "1e3b4842bde9088cc96a5fafa7fc134e"


def good_profile(name: str = "u1", **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "secret": SECRET,
        "backend": "127.64.0.2:2400",
        "carrier_mode": "https",
        **kw,
    }


def put_cfg(fake: FakeSystemOps, profiles: Any, cfg: Any = None, mode: int = 0o400) -> None:
    fake.put_file(CFG, json.dumps(cfg if cfg is not None else {"limits": {"max_profiles": 3}}))
    fake.put_file(PRF, profiles if isinstance(profiles, bytes) else json.dumps(profiles), mode=mode)


async def check(fake: FakeSystemOps) -> tuple[bool, str]:
    r = await fake.tproxy_check(CFG, PRF)
    return r.ok, r.output


# --- filesystem -----------------------------------------------------------------------


async def test_fs_roundtrip() -> None:
    f = FakeSystemOps()
    await f.write_atomic("/etc/x/y.txt", b"hi", mode=0o640, owner="root", group="tproxy")
    assert await f.read_file("/etc/x/y.txt") == b"hi"
    st = await f.stat("/etc/x/y.txt")
    assert (st.mode, st.owner, st.group, st.size) == (0o640, "root", "tproxy", 2)
    assert await f.exists("/etc/x") and await f.exists("/etc/x/y.txt")
    assert await f.list_dir("/etc") == ["x"]
    assert await f.list_dir("/etc/x") == ["y.txt"]
    await f.remove("/etc/x/y.txt")
    await f.remove("/etc/x/y.txt")
    with pytest.raises(SystemOpsError):
        await f.read_file("/etc/x/y.txt")
    with pytest.raises(SystemOpsError):
        await f.list_dir("/nope")
    with pytest.raises(SystemOpsError):
        await f.read_file("relative")


async def test_strict_dirs() -> None:
    f = FakeSystemOps(strict_dirs=True)
    with pytest.raises(SystemOpsError):
        await f.write_atomic("/var/backups/a", b"", mode=0o600, owner="root", group="root")
    f.mkdir("/var/backups")
    await f.write_atomic("/var/backups/a", b"", mode=0o600, owner="root", group="root")


async def test_tar_in_fs() -> None:
    f = FakeSystemOps()
    await f.make_tar_gz("/var/backups/a.tar.gz", {"x/y": b"1"})
    assert (await f.stat("/var/backups/a.tar.gz")).mode == 0o600
    assert await f.read_tar_gz("/var/backups/a.tar.gz") == {"x/y": b"1"}
    with pytest.raises(SystemOpsError):
        await f.make_tar_gz("/a.tar.gz", {"../evil": b""})


async def test_calls_never_log_file_data() -> None:
    f = FakeSystemOps()
    await f.write_atomic("/a", SECRET.encode(), mode=0o600, owner="root", group="root")
    assert SECRET not in repr(f.calls)


# --- systemd --------------------------------------------------------------------------


async def test_systemd_state_machine() -> None:
    f = FakeSystemOps()
    await f.systemctl("enable-now", "tgpanel-mtproxy@1.service")
    assert await f.is_active("tgpanel-mtproxy@1")
    assert "tgpanel-mtproxy@1" in f.enabled
    await f.systemctl("restart", "tgpanel-mtproxy@1")
    assert f.restart_count("tgpanel-mtproxy@1.service") == 1
    assert await f.unit_property("tgpanel-mtproxy@1", "NRestarts") == "1"
    assert await f.unit_property("tgpanel-mtproxy@1", "ActiveState") == "active"
    await f.systemctl("disable-now", "tgpanel-mtproxy@1")
    assert not await f.is_active("tgpanel-mtproxy@1")
    await f.systemctl("mask-now", "mtproxy.service")
    assert await f.unit_property("mtproxy", "UnitFileState") == "masked"
    with pytest.raises(SystemOpsError):
        await f.systemctl("start", "mtproxy")
    await f.systemctl("unmask", "mtproxy")
    await f.systemctl("start", "mtproxy")
    await f.systemctl("daemon-reload", "")
    assert f.daemon_reloads == 1
    assert f.systemctl_calls("restart") == [("restart", "tgpanel-mtproxy@1")]
    with pytest.raises(SystemOpsError):
        await f.systemctl("reboot", "x")
    with pytest.raises(SystemOpsError):
        await f.systemctl("start", "bad unit")


# --- failure injection ----------------------------------------------------------------


async def test_fail_on_match_times_skip() -> None:
    f = FakeSystemOps()
    f.fail_on("systemctl", "restart tproxy-server")
    await f.systemctl("restart", "caddy")
    with pytest.raises(SystemOpsError):
        await f.systemctl("restart", "tproxy-server")
    await f.systemctl("restart", "tproxy-server")  # times=1 exhausted
    assert f.call_count("systemctl", "restart tproxy-server") == 2
    assert f.restart_count("tproxy-server") == 1  # failed call had no effect

    f.fail_on("write_atomic", "/etc/a", skip=1, times=2, exc=RuntimeError)
    kw: dict[str, Any] = {"mode": 0o600, "owner": "root", "group": "root"}
    await f.write_atomic("/etc/a", b"1", **kw)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await f.write_atomic("/etc/a", b"2", **kw)
    await f.write_atomic("/etc/a", b"3", **kw)
    assert f.get_text("/etc/a") == "3"

    f.fail_on("read_file", lambda args: args[0].endswith(".json"), times=None)
    with pytest.raises(SystemOpsError):
        await f.read_file("/x.json")
    with pytest.raises(SystemOpsError):
        await f.read_file("/x.json")
    f.clear_failures()
    f.put_file("/x.json", b"{}")
    assert await f.read_file("/x.json") == b"{}"


async def test_fail_check() -> None:
    f = FakeSystemOps()
    put_cfg(f, {"profiles": [good_profile()]})
    f.fail_check("tproxy_check", "boom")
    assert await check(f) == (False, "boom")
    assert (await check(f))[0]
    f.put_file("/etc/caddy/Caddyfile", "a {\n}\n")
    f.fail_check("caddy_validate")
    assert not (await f.caddy_validate("/etc/caddy/Caddyfile", {})).ok
    assert (await f.caddy_validate("/etc/caddy/Caddyfile", {"X": "1"})).ok
    assert f.last_caddy_env == {"X": "1"}


# --- tproxy_check ---------------------------------------------------------------------


async def test_tproxy_check_valid() -> None:
    f = FakeSystemOps()
    put_cfg(f, {"profiles": [good_profile("u1"), good_profile("u2", secret="dd" + SECRET)]})
    assert await check(f) == (True, "ok")


@pytest.mark.parametrize(
    ("profiles", "needle"),
    [
        (b"{not json", "invalid JSON"),
        ({"profiles": []}, "no profiles"),
        ({"profiles": [good_profile("a"), good_profile("a")]}, "duplicate"),
        ({"profiles": [good_profile(extra=1)]}, "unknown field"),
        ({"profiles": [good_profile(secret="xyz")]}, "bad secret"),
        ({"profiles": [good_profile(backend="10.0.0.1:2400")]}, "loopback"),
        ({"profiles": [good_profile(backend="localhost:2400")]}, "loopback"),
        ({"profiles": [good_profile(backend="127.0.0.1:99999")]}, "port"),
        ({"profiles": [good_profile(carrier_mode="udp")]}, "carrier_mode"),
        ({"profiles": [good_profile("x" * 65)]}, "name"),
        ({"profiles": [good_profile("")]}, "name"),
        ({"profiles": [good_profile(str(i)) for i in range(4)]}, "max_profiles"),
        ({"profiles": [good_profile()], "other": 1}, "top level"),
    ],
)
async def test_tproxy_check_invalid(profiles: Any, needle: str) -> None:
    f = FakeSystemOps()
    put_cfg(f, profiles)
    ok, out = await check(f)
    assert not ok
    assert needle in out
    assert SECRET not in out


async def test_tproxy_check_limits_modes_and_defaults() -> None:
    f = FakeSystemOps()
    # default max_profiles = 32 when absent
    put_cfg(f, {"profiles": [good_profile(str(i)) for i in range(32)]}, cfg={})
    assert (await check(f))[0]
    put_cfg(f, {"profiles": [good_profile(str(i)) for i in range(33)]}, cfg={})
    assert "max_profiles" in (await check(f))[1]
    put_cfg(f, {"profiles": [good_profile()]}, mode=0o644)
    assert "mode" in (await check(f))[1]
    put_cfg(f, {"profiles": [good_profile()]}, mode=0o640)
    assert (await check(f))[0]  # group read (systemd credential) is allowed
    put_cfg(f, {"profiles": [good_profile()]}, mode=0o660)
    assert "mode" in (await check(f))[1]
    put_cfg(f, {"profiles": [good_profile()]}, mode=0o404)
    assert "mode" in (await check(f))[1]
    put_cfg(f, {"profiles": [good_profile()]}, cfg=[1])
    assert not (await check(f))[0]
    put_cfg(f, {"profiles": [good_profile()]}, cfg={"limits": {"max_profiles": 0}})
    assert not (await check(f))[0]
    assert not (await f.tproxy_check("/nope1", "/nope2")).ok


async def test_tproxy_check_on_seeded_fixtures() -> None:
    for variant in ("clean", "owner"):
        f = FakeSystemOps()
        f.seed_upstream(variant)
        res = await f.tproxy_check(
            "/etc/tproxy-server/config.json", "/etc/tproxy-server/profiles.json"
        )
        assert res.ok, res.output


# --- caddy ----------------------------------------------------------------------------


async def test_caddy_validate() -> None:
    f = FakeSystemOps()
    f.seed_upstream("clean")
    assert (await f.caddy_validate("/etc/caddy/Caddyfile", {"TPROXY_HOSTNAME": "h"})).ok
    f.put_file("/c1", "a {\n  b {\n}\n")
    assert not (await f.caddy_validate("/c1", {})).ok
    f.put_file("/c2", "a {\n}\n# } stray in comment\n")
    assert (await f.caddy_validate("/c2", {})).ok
    f.put_file("/c3", "a {\n}\n")
    f.mark_caddyfile_invalid("/c3")
    assert not (await f.caddy_validate("/c3", {})).ok
    assert not (await f.caddy_validate("/missing", {})).ok


# --- seed -----------------------------------------------------------------------------


async def test_seed_upstream_owner() -> None:
    f = FakeSystemOps()
    f.seed_upstream("owner")
    st = await f.stat("/etc/tproxy-server/profiles.json")
    assert (st.mode, st.owner, st.group) == (0o400, "root", "tproxy")
    assert (await f.stat("/etc/tproxy-server/config.json")).mode == 0o640
    assert await f.exists("/etc/mtproxy/mtproxy.secrets")
    assert await f.exists("/etc/systemd/system/mtproxy.service.d/nat.conf")
    assert await f.exists("/etc/systemd/system/caddy.service.d/tproxy.conf")
    assert {"tproxy-server", "mtproxy", "caddy"} <= f.active
    assert await f.is_active("caddy.service")
    assert await f.wait_tcp_open("127.0.0.1", 8081, 1)
    assert (await f.http_get("http://127.0.0.1:8081/readyz", 1)).status == 200
    clean = FakeSystemOps()
    clean.seed_upstream("clean")
    assert not await clean.exists("/etc/mtproxy/mtproxy.secrets")


# --- ports / http / dns / cert --------------------------------------------------------


async def test_pool_ports() -> None:
    f = FakeSystemOps()
    unit = "tgpanel-mtproxy@1.service"
    await f.systemctl("start", unit)
    assert not await f.wait_tcp_open("127.0.0.1", 2400, 1)  # no env file yet
    f.put_file("/etc/tgpanel/mtproxy/1.env", "MTP_PORT=2400\nMTP_STATS_PORT=8900\n")
    assert await f.wait_tcp_open("127.0.0.1", 2400, 1)
    await f.systemctl("stop", unit)
    assert not await f.wait_tcp_open("127.0.0.1", 2400, 1)
    f.set_port_open(2400)
    assert await f.wait_tcp_open("127.0.0.1", 2400, 1)
    f.set_port_open(2400, False)
    await f.systemctl("start", unit)
    assert not await f.wait_tcp_open("127.0.0.1", 2400, 1)  # forced closed wins


async def test_http_config_and_scripts() -> None:
    f = FakeSystemOps()
    u = "http://127.0.0.1:8081"
    for p in ("/healthz", "/readyz", "/metrics"):
        assert (await f.http_get(u + p, 1)).status == 200
    assert (await f.http_get(u + "/other", 1)).status == 404
    f.set_http("/readyz", 503)
    assert (await f.http_get(u + "/readyz", 1)).status == 503
    assert (await f.http_get(u + "/healthz", 1)).status == 200
    f.queue_http("/healthz", [HttpResult(0), HttpResult(0)])
    assert [(await f.http_get(u + "/healthz", 1)).status for _ in range(3)] == [0, 0, 200]


async def test_dns_ip_cert() -> None:
    f = FakeSystemOps()
    assert await f.resolve("nope.example") == ([], [])
    f.set_dns("p.example", a=["203.0.113.10"], aaaa=["2001:db8::1"])
    assert await f.resolve("p.example") == (["203.0.113.10"], ["2001:db8::1"])
    assert await f.public_ipv4() == "203.0.113.10"
    f.public_ip = None
    assert await f.public_ipv4() is None
    cert = CertInfo("Let's Encrypt", "2026-01-01T00:00:00Z", "2026-04-01T00:00:00Z", True)
    assert await f.tls_cert_info("p.example") is None
    f.queue_cert("p.example", [None, cert])
    assert await f.tls_cert_info("p.example") is None
    assert await f.tls_cert_info("p.example") == cert
    f.set_cert("p.example", cert)
    assert await f.tls_cert_info("p.example") == cert


# --- nft ------------------------------------------------------------------------------


async def test_nft() -> None:
    f = FakeSystemOps()
    await f.nft_add_elements("tgpanel", "up", ["127.64.0.2", "127.64.0.3"])
    await f.nft_add_elements("tgpanel", "down", ["127.64.0.2"])
    f.add_traffic("127.64.0.2", up_bytes=100, up_packets=2, down_bytes=50, down_packets=1)
    f.add_traffic("127.64.0.2", up_bytes=20, up_packets=1)
    assert (await f.nft_list_set("tgpanel", "up"))["127.64.0.2"] == SetCounter(120, 3)
    assert (await f.nft_list_set("tgpanel", "down"))["127.64.0.2"] == SetCounter(50, 1)
    await f.nft_add_elements("tgpanel", "up", ["127.64.0.2"])  # re-add keeps counters
    assert (await f.nft_list_set("tgpanel", "up"))["127.64.0.2"].bytes == 120
    await f.nft_delete_elements("tgpanel", "up", ["127.64.0.3"])
    assert set(await f.nft_list_set("tgpanel", "up")) == {"127.64.0.2"}
    with pytest.raises(SystemOpsError):
        await f.nft_delete_elements("tgpanel", "up", ["127.64.0.3"])
    with pytest.raises(SystemOpsError):
        await f.nft_list_set("tgpanel", "missing")
    with pytest.raises(SystemOpsError):
        await f.nft_add_elements("tgpanel", "up", ["not-an-ip"])
    with pytest.raises(ValueError):
        f.add_traffic("127.64.0.99", up_bytes=1)


async def test_nft_load_creates_sets() -> None:
    f = FakeSystemOps()
    f.put_file(
        "/etc/tgpanel/tgpanel.nft",
        "table inet other {\n  set a { type ipv4_addr; counter; }\n}\n",
    )
    await f.nft_load_file("/etc/tgpanel/tgpanel.nft")
    assert await f.nft_list_set("other", "a") == {}
    assert len(f.nft_loaded) == 1


# --- locks ----------------------------------------------------------------------------


async def test_lock_semantics() -> None:
    f = FakeSystemOps()
    h = await f.acquire_lock("/run/tgpanel/apply.lock", 1)
    assert f.is_locked("/run/tgpanel/apply.lock")
    with pytest.raises(SystemOpsError):
        await f.acquire_lock("/run/tgpanel/apply.lock", 0.05)
    other = await f.acquire_lock("/run/tgpanel/other.lock", 1)  # independent path
    await other.release()
    order: list[str] = []

    async def second() -> None:
        h2 = await f.acquire_lock("/run/tgpanel/apply.lock", 2)
        order.append("second-acquired")
        await h2.release()

    t = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert order == []
    order.append("release")
    await h.release()
    await h.release()  # idempotent
    await asyncio.wait_for(t, 1)
    assert order == ["release", "second-acquired"]
    assert not f.is_locked("/run/tgpanel/apply.lock")
