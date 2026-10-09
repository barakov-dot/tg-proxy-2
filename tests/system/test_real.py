"""RealSystemOps: parts that can run anywhere without root (no systemctl/nft/caddy calls)."""

from __future__ import annotations

import asyncio
import getpass
import grp
import os
import stat
from pathlib import Path

import pytest

from tgpanel.system.ops import SetCounter, SystemOpsError
from tgpanel.system.real import RealSystemOps, parse_nft_set_json, run_command

_SECRET = "1e3b4842bde9088cc96a5fafa7fc134e"
NFT_SAMPLE = """
{"nftables": [
  {"metainfo": {"version": "1.0.9", "release_name": "Old Doc Yak #3", "json_schema_version": 1}},
  {"set": {"family": "inet", "name": "up", "table": "tgpanel", "type": "ipv4_addr",
           "handle": 3, "flags": ["interval"],
           "elem": [
             {"elem": {"val": "127.64.0.2", "counter": {"packets": 3, "bytes": 180}}},
             {"elem": {"val": "127.64.0.3", "counter": {"packets": 0, "bytes": 0}}},
             "127.64.0.9",
             {"prefix": {"addr": "10.0.0.0", "len": 8}}
           ]}}
]}
"""


def _me() -> tuple[str, str]:
    return getpass.getuser(), grp.getgrgid(os.getegid()).gr_name


def test_parse_nft() -> None:
    got = parse_nft_set_json(NFT_SAMPLE)
    assert got == {
        "127.64.0.2": SetCounter(bytes=180, packets=3),
        "127.64.0.3": SetCounter(bytes=0, packets=0),
        "127.64.0.9": SetCounter(bytes=0, packets=0),
    }


def test_parse_nft_empty_and_invalid() -> None:
    empty = '{"nftables": [{"metainfo": {}}, {"set": {"name": "up"}}]}'
    assert parse_nft_set_json(empty) == {}
    with pytest.raises(SystemOpsError):
        parse_nft_set_json("not json")
    with pytest.raises(SystemOpsError):
        parse_nft_set_json("[]")


async def test_run_command_real_commands() -> None:
    ok = await run_command(["true"])
    assert ok.ok
    bad = await run_command(["false"])
    assert not bad.ok and bad.returncode == 1
    echo = await run_command(["echo", "hello", "1e3b4842bde9088cc96a5fafa7fc134e"])
    assert "hello" in echo.output
    assert "1e3b4842bde9088cc96a5fafa7fc134e" not in echo.output
    assert "[redacted]" in echo.output
    # no shell: metacharacters are plain arguments
    meta = await run_command(["echo", "a; echo pwned $(id)"])
    assert meta.stdout.decode().strip() == "a; echo pwned $(id)"


async def test_run_command_timeout_and_missing() -> None:
    res = await run_command(["sleep", "5"], timeout_s=0.2)
    assert res.timed_out and not res.ok
    with pytest.raises(SystemOpsError):
        await run_command(["/nonexistent/binary"])


async def test_systemctl_uses_injected_binary_and_validates(tmp_path: Path) -> None:
    log = tmp_path / "log"
    script = tmp_path / "fakectl"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        f'[ "$2" = "boom" ] && echo "secret {_SECRET}" && exit 3\nexit 0\n'
    )
    script.chmod(0o755)
    ops = RealSystemOps(systemctl_bin=str(script))
    await ops.systemctl("enable-now", "tgpanel-mtproxy@1.service")
    assert log.read_text().strip() == "enable --now tgpanel-mtproxy@1.service"
    assert await ops.is_active("x")
    with pytest.raises(SystemOpsError):
        await ops.systemctl("restart", "bad unit")
    with pytest.raises(SystemOpsError):
        await ops.systemctl("poweroff", "x")
    with pytest.raises(SystemOpsError) as ei:
        await ops.systemctl("restart", "boom")
    assert "1e3b4842" not in str(ei.value)
    assert "[redacted]" in str(ei.value)


async def test_nft_args_via_injected_binary(tmp_path: Path) -> None:
    log = tmp_path / "log"
    script = tmp_path / "fakenft"
    script.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a" >> {log}; done; echo >> {log}\n'
    )
    script.chmod(0o755)
    ops = RealSystemOps(nft_bin=str(script))
    await ops.nft_add_elements("tgpanel", "up", ["127.64.0.2", "127.64.0.3"])
    await ops.nft_delete_elements("tgpanel", "down", ["127.64.0.2"])
    await ops.nft_add_elements("tgpanel", "up", [])
    lines = log.read_text().splitlines()
    assert lines == [
        "[add][element][inet][tgpanel][up][{ 127.64.0.2, 127.64.0.3 }]",
        "[delete][element][inet][tgpanel][down][{ 127.64.0.2 }]",
    ]
    for table, name, ips in (
        ("t;x", "up", ["127.0.0.1"]),
        ("t", "up", ["127.0.0.1, 1.1.1.1; flush ruleset"]),
        ("t", "u p", ["127.0.0.1"]),
    ):
        with pytest.raises(SystemOpsError):
            await ops.nft_add_elements(table, name, ips)
    with pytest.raises(SystemOpsError):
        await ops.nft_load_file("relative.nft")


async def test_tproxy_check_command_line(tmp_path: Path) -> None:
    script = tmp_path / "fakeproxy"
    script.write_text('#!/bin/sh\necho "args: $@"\nexit 1\n')
    script.chmod(0o755)
    ops = RealSystemOps(tproxy_bin=str(script))
    res = await ops.tproxy_check("/work/c.json", "/work/p.json")
    assert not res.ok
    assert res.output == "args: -config /work/c.json -profiles-file /work/p.json -check"


async def test_caddy_validate_env_merged(tmp_path: Path) -> None:
    script = tmp_path / "fakecaddy"
    script.write_text('#!/bin/sh\necho "$@ $TPROXY_HOSTNAME $PATH"\n')
    script.chmod(0o755)
    ops = RealSystemOps(caddy_bin=str(script))
    res = await ops.caddy_validate("/etc/caddy/Caddyfile", {"TPROXY_HOSTNAME": "h.example.com"})
    assert res.ok
    assert res.output.startswith(
        "validate --config /etc/caddy/Caddyfile --adapter caddyfile h.example.com"
    )
    with pytest.raises(SystemOpsError):
        await ops.caddy_validate("/etc/caddy/Caddyfile", {"A B": "x"})


async def test_write_atomic_and_files(tmp_path: Path) -> None:
    ops = RealSystemOps()
    owner, group = _me()
    p = str(tmp_path / "secret.json")
    await ops.write_atomic(p, b"v1", mode=0o400, owner=owner, group=group)
    st = await ops.stat(p)
    assert (st.mode, st.owner, st.group, st.size) == (0o400, owner, group, 2)
    await ops.write_atomic(p, b"v2!", mode=0o600, owner=owner, group=group)
    assert await ops.read_file(p) == b"v2!"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert await ops.list_dir(str(tmp_path)) == ["secret.json"]  # no temp leftovers
    assert await ops.exists(p)
    await ops.remove(p)
    await ops.remove(p)  # idempotent
    assert not await ops.exists(p)
    with pytest.raises(SystemOpsError):
        await ops.read_file(p)
    with pytest.raises(SystemOpsError):
        await ops.write_atomic(p, b"", mode=0o600, owner="no-such-user-xyz", group=group)
    with pytest.raises(SystemOpsError):
        await ops.write_atomic(
            str(tmp_path / "nodir" / "f"), b"", mode=0o600, owner=owner, group=group
        )
    with pytest.raises(SystemOpsError):
        await ops.write_atomic("rel", b"", mode=0o600, owner=owner, group=group)


async def test_write_atomic_never_wider_than_requested(tmp_path: Path) -> None:
    ops = RealSystemOps()
    owner, group = _me()
    seen: list[int] = []
    p = tmp_path / "f"
    orig = os.replace

    def spy(src: str, dst: str) -> None:
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        orig(src, dst)

    import unittest.mock as m

    with m.patch("os.replace", spy):
        await ops.write_atomic(str(p), b"x", mode=0o400, owner=owner, group=group)
    assert seen == [0o400]  # mode already final at rename time


async def test_tar_files(tmp_path: Path) -> None:
    ops = RealSystemOps()
    dest = str(tmp_path / "b.tar.gz")
    members = {"etc/profiles.json": b"{}", "db.sqlite": b"\x00\x01"}
    await ops.make_tar_gz(dest, members)
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert await ops.read_tar_gz(dest) == members
    with pytest.raises(SystemOpsError):
        await ops.make_tar_gz(dest, {"../x": b""})
    (tmp_path / "bad.tar.gz").write_bytes(b"junk")
    with pytest.raises(SystemOpsError):
        await ops.read_tar_gz(str(tmp_path / "bad.tar.gz"))


async def test_lock_behaviour(tmp_path: Path) -> None:
    ops = RealSystemOps()
    p = str(tmp_path / "l.lock")
    h1 = await ops.acquire_lock(p, 1)
    with pytest.raises(SystemOpsError):
        await ops.acquire_lock(p, 0.2)
    waiter = asyncio.create_task(ops.acquire_lock(p, 2))
    await asyncio.sleep(0.1)
    assert not waiter.done()
    await h1.release()
    await h1.release()  # idempotent
    h2 = await asyncio.wait_for(waiter, 2)
    await h2.release()
    h3 = await ops.acquire_lock(p, 0.2)
    await h3.release()


async def test_wait_tcp_open() -> None:
    ops = RealSystemOps()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await ops.wait_tcp_open("127.0.0.1", port, 1.0)
    finally:
        server.close()
        await server.wait_closed()
    assert not await ops.wait_tcp_open("127.0.0.1", port, 0.5)


async def test_http_get_local() -> None:
    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n")
        await w.drain()
        w.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ops = RealSystemOps()
        res = await ops.http_get(f"http://127.0.0.1:{port}/healthz", 2)
        assert (res.status, res.body) == (200, b"ok\n")
    finally:
        server.close()
        await server.wait_closed()
    assert (await RealSystemOps().http_get(f"http://127.0.0.1:{port}/", 1)).status == 0
    with pytest.raises(SystemOpsError):
        await RealSystemOps().http_get("file:///etc/passwd", 1)


async def test_public_ipv4_failure_is_none() -> None:
    ops = RealSystemOps(ip_probe_urls=["http://127.0.0.1:1/"])
    assert await ops.public_ipv4() is None


async def test_resolve_localhost_and_bad_host() -> None:
    ops = RealSystemOps()
    a, _ = await ops.resolve("localhost")
    assert "127.0.0.1" in a
    with pytest.raises(SystemOpsError):
        await ops.resolve("a b")


async def test_caddy_validate_sets_throwaway_xdg_dirs(tmp_path: Path) -> None:
    script = tmp_path / "fakecaddy"
    script.write_text('#!/bin/sh\necho "$XDG_DATA_HOME $XDG_CONFIG_HOME"\n')
    script.chmod(0o755)
    ops = RealSystemOps(caddy_bin=str(script))
    res = await ops.caddy_validate("/etc/caddy/Caddyfile", {})
    assert res.output == "/run/tgpanel/caddy-validate /run/tgpanel/caddy-validate"


async def test_real_disk_usage_uses_statvfs(tmp_path: Path) -> None:
    usage = await RealSystemOps().disk_usage(str(tmp_path))
    st = os.statvfs(tmp_path)
    assert usage.total == st.f_blocks * st.f_frsize and 0 <= usage.used <= usage.total
    with pytest.raises(SystemOpsError):
        await RealSystemOps().disk_usage(str(tmp_path / "missing"))
