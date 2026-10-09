# ruff: noqa: E501
"""ServerMetricsService: /proc parsing, deltas, resets, interface choice, caching."""

from __future__ import annotations

import pytest

from tgpanel.services import server_metrics as sm
from tgpanel.services.server_metrics import ServerMetricsService
from tgpanel.system.fake import FakeSystemOps
from tgpanel.system.ops import DiskUsage

STAT = """cpu  10132153 290696 3084719 46828483 16683 0 25195 0 0 0
cpu0 1393280 32966 572056 13343292 6130 0 17875 0 0 0
cpu1 1335500 38271 464004 1347289 3040 0 2891 0 0 0
intr 1462898 0 10 0
ctxt 2 3
"""
MEMINFO = """MemTotal:       16384000 kB
MemFree:         1000000 kB
MemAvailable:    8192000 kB
Buffers:          500000 kB
Cached:          4000000 kB
SwapTotal:             0 kB
"""
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000000   100    0    0    0     0          0         0  5000000    100    0    0    0     0       0          0
  eth0: 1000000   200    0    0    0     0          0         0   400000    150    0    0    0     0       0          0
 veth1: 9999999999 1    0    0    0     0          0         0 9999999999    1    0    0    0     0       0          0
docker0: 888888888 1    0    0    0     0          0         0 888888888    1    0    0    0     0       0          0
"""
ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT
eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0000000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""


def test_parse_cpu_uses_busy_over_total_and_counts_cores() -> None:
    busy, total, cores = sm.parse_cpu(STAT) or (0, 0, 0)
    idle = 46828483 + 16683
    assert total == 10132153 + 290696 + 3084719 + 46828483 + 16683 + 0 + 25195 + 0
    assert busy == total - idle and cores == 2
    assert sm.parse_cpu("garbage") is None and sm.parse_cpu("cpu  1 2") is None


def test_parse_meminfo_available_and_fallback() -> None:
    assert sm.parse_meminfo(MEMINFO) == ((16384000 - 8192000) * 1024, 16384000 * 1024)
    no_avail = "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n"
    assert sm.parse_meminfo(no_avail) == (500 * 1024, 1000 * 1024)
    assert sm.parse_meminfo("nothing") is None


def test_parse_loadavg_uptime() -> None:
    assert sm.parse_loadavg("0.52 0.40 0.31 1/234 5678\n") == (0.52, 0.40, 0.31)
    assert sm.parse_loadavg("") is None
    assert sm.parse_uptime("12345.67 9999.1\n") == 12345 and sm.parse_uptime("x") is None


def test_parse_netdev_route_and_interface_choice() -> None:
    counters = sm.parse_netdev(NETDEV)
    assert counters["eth0"] == (1000000, 400000) and "lo" in counters
    assert sm.parse_default_iface(ROUTE) == "eth0"
    assert sm.parse_default_iface("Iface\tDestination\n") is None
    assert sm.pick_interface(counters, "eth0") == "eth0"
    assert sm.pick_interface(counters, None) == "eth0"  # veth/docker/lo are never picked
    assert sm.pick_interface(counters, "docker0") == "eth0"
    assert sm.pick_interface({"lo": (1, 1), "veth9": (5, 5)}, None) is None
    busy = {"eth0": (10, 10), "ens3": (500, 500)}
    assert sm.pick_interface(busy, None) == "ens3"  # no default route: most traffic


def test_counter_delta_reset_and_32bit_wrap() -> None:
    assert sm.counter_delta(100, 160) == 60
    assert sm.counter_delta(100, 100) == 0
    assert sm.counter_delta(5_000_000, 10) is None  # reset (interface restarted)
    assert sm.counter_delta(2**32 - 100, 50) == 150  # 32-bit counter wrapped


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def seed(
    fake: FakeSystemOps, *, busy: int, idle: int, rx: int, tx: int, iface: str = "eth0"
) -> None:
    fake.files["/proc/stat"] = _file(
        f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n"
    )
    fake.files["/proc/meminfo"] = _file(MEMINFO)
    fake.files["/proc/loadavg"] = _file("0.10 0.20 0.30 1/2 3\n")
    fake.files["/proc/uptime"] = _file("86400.5 1.0\n")
    fake.files["/proc/net/dev"] = _file(
        f"h1\nh2\n  {iface}: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n    lo: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n"
    )
    fake.files["/proc/net/route"] = _file(
        f"Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n{iface}\t00000000\t01\t0003\t0\t0\t0\t0\n"
    )


def _file(text: str):  # type: ignore[no-untyped-def]
    from tgpanel.system.fake import FakeFile

    return FakeFile(text.encode(), 0o444, "root", "root")


async def no_sleep(_: float) -> None:
    return None


async def test_first_snapshot_takes_baseline_then_deltas() -> None:
    fake = FakeSystemOps()
    clock = Clock()
    seed(fake, busy=100, idle=900, rx=1_000_000, tx=0)
    svc = ServerMetricsService(fake, monotonic=clock, sleep=no_sleep)
    first = await svc.snapshot()
    assert first.mem_total == 16384000 * 1024 and first.cores == 1
    assert first.uptime_s == 86400 and first.load == (0.10, 0.20, 0.30)
    assert (first.net_iface == "eth0" and first.cpu_pct is None) or first.cpu_pct == 0.0
    # second sample: +50 busy, +50 idle jiffies = 50 %; 5 s later 1.25 MB in / 0.5 MB out
    clock.t += 5.0
    seed(fake, busy=150, idle=950, rx=2_250_000, tx=500_000)
    view = await svc.snapshot()
    assert view.cpu_pct == pytest.approx(50.0)
    assert view.rx_mbit == pytest.approx(2.0) and view.tx_mbit == pytest.approx(0.8)
    assert view.mem_used == 8192000 * 1024 and view.mem_pct == pytest.approx(50.0)


async def test_snapshot_is_cached_for_a_moment() -> None:
    fake = FakeSystemOps()
    clock = Clock()
    seed(fake, busy=1, idle=9, rx=1, tx=1)
    svc = ServerMetricsService(fake, monotonic=clock, sleep=no_sleep)
    await svc.snapshot()
    reads = fake.call_count("read_file")
    clock.t += 0.5
    await svc.snapshot()
    assert fake.call_count("read_file") == reads
    clock.t += 5
    await svc.snapshot()
    assert fake.call_count("read_file") > reads


async def test_counter_reset_and_no_interface() -> None:
    fake = FakeSystemOps()
    clock = Clock()
    seed(fake, busy=100, idle=900, rx=9_000_000, tx=9_000_000)
    svc = ServerMetricsService(fake, monotonic=clock, sleep=no_sleep)
    await svc.snapshot()
    clock.t += 5
    seed(fake, busy=100, idle=900, rx=10, tx=10)  # counters went backwards (reset)
    view = await svc.snapshot()
    assert view.rx_mbit is None and view.tx_mbit is None and view.cpu_pct is None
    clock.t += 5
    seed(fake, busy=200, idle=1000, rx=1_000_010, tx=1_000_010)
    view = await svc.snapshot()  # the reset sample became the new baseline
    assert view.rx_mbit == pytest.approx(1.6) and view.cpu_pct == pytest.approx(50.0)
    clock.t += 5
    fake.files["/proc/net/dev"] = _file("h\nh\n    lo: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n")
    none = await svc.snapshot()
    assert none.net_iface is None and none.rx_mbit is None


async def test_missing_proc_files_degrade_gracefully() -> None:
    fake = FakeSystemOps()
    svc = ServerMetricsService(fake, monotonic=Clock(), sleep=no_sleep)
    view = await svc.snapshot()
    assert view.cpu_pct is None and view.mem_total is None and view.uptime_s is None
    assert {"cpu", "memory", "network"} <= set(view.errors)
    assert view.disks[0].path == "/"  # the fake has a root disk by default


async def test_disks_dedupe_same_filesystem_and_split_mounts() -> None:
    fake = FakeSystemOps()
    seed(fake, busy=1, idle=9, rx=1, tx=1)
    svc = ServerMetricsService(fake, monotonic=Clock(), sleep=no_sleep)
    assert [d.path for d in (await svc.snapshot()).disks] == ["/"]  # /var/lib/tgpanel is on "/"
    fake.disks["/var/lib/tgpanel"] = DiskUsage(50 * 2**30, 5 * 2**30)
    svc2 = ServerMetricsService(fake, monotonic=Clock(), sleep=no_sleep)
    disks = (await svc2.snapshot()).disks
    assert [d.path for d in disks] == ["/", "/var/lib/tgpanel"]
    assert disks[0].pct == pytest.approx(40.0) and disks[1].pct == pytest.approx(10.0)


async def test_fake_disk_usage_longest_mount_wins() -> None:
    fake = FakeSystemOps()
    fake.disks["/var"] = DiskUsage(10, 1)
    assert (await fake.disk_usage("/var/lib/x")).total == 10
    assert (await fake.disk_usage("/etc")).total == 100 * 2**30
