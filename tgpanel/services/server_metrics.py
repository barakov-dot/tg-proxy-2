"""Server metrics for the dashboard: CPU, memory, disk, network, load, uptime.

Everything comes from ``/proc`` (read through ``SystemOps.read_file``) plus
``SystemOps.disk_usage``; no commands are run. CPU % and network rates are deltas between two
samples kept in the service (the first call takes a short baseline pair). Results are cached
for ``CACHE_S`` seconds so several open tabs cost one sample.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tgpanel.system.ops import SystemOps, SystemOpsError

CACHE_S = 2.0
BASELINE_WAIT_S = 0.25
DISK_PATHS = ("/", "/var/lib/tgpanel")
VIRTUAL_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "cni", "flannel", "cali", "dummy")
WRAP_32 = 2**32


@dataclass(frozen=True, slots=True)
class DiskView:
    path: str
    used: int
    total: int

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class MetricsView:
    cpu_pct: float | None = None
    cores: int | None = None
    load: tuple[float, float, float] | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    disks: tuple[DiskView, ...] = ()
    net_iface: str | None = None
    rx_mbit: float | None = None  # inbound to the server, Mbit/s
    tx_mbit: float | None = None
    uptime_s: int | None = None
    errors: tuple[str, ...] = field(default=())

    @property
    def mem_pct(self) -> float:
        if not self.mem_total:
            return 0.0
        return 100.0 * (self.mem_used or 0) / self.mem_total


# --------------------------------------------------------------------------------- parsers


def parse_cpu(text: str) -> tuple[int, int, int] | None:
    """(busy, total, cores) jiffies of the aggregate ``cpu`` line of ``/proc/stat``."""
    cores = sum(1 for ln in text.splitlines() if ln.startswith("cpu") and ln[3:4].isdigit())
    for line in text.splitlines():
        if line.startswith("cpu "):
            try:
                v = [int(x) for x in line.split()[1:]]
            except ValueError:
                return None
            if len(v) < 4:
                return None
            v += [0] * (8 - len(v)) if len(v) < 8 else []
            idle = v[3] + v[4]  # idle + iowait
            total = sum(v[:8])  # guest time is already inside user/nice
            return total - idle, total, cores
    return None


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """(used, total) bytes; used = total - MemAvailable (free + buffers + cache as fallback)."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    total = values.get("MemTotal")
    if not total:
        return None
    if "MemAvailable" in values:
        available = values["MemAvailable"]
    else:
        available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
    return max(total - available, 0), total


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def parse_uptime(text: str) -> int | None:
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def parse_netdev(text: str) -> dict[str, tuple[int, int]]:
    """interface -> (rx_bytes, tx_bytes)."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        if len(fields) >= 9 and fields[0].isdigit() and fields[8].isdigit():
            out[name.strip()] = (int(fields[0]), int(fields[8]))
    return out


def parse_default_iface(text: str) -> str | None:
    """Interface of the default route (lowest metric) from ``/proc/net/route``."""
    best: tuple[int, str] | None = None
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8 or cols[1] != "00000000":
            continue
        try:
            flags, metric = int(cols[3], 16), int(cols[6])
        except ValueError:
            continue
        if flags & 1 and (best is None or metric < best[0]):  # RTF_UP
            best = (metric, cols[0])
    return None if best is None else best[1]


def is_virtual(name: str) -> bool:
    return name.startswith(VIRTUAL_PREFIXES)


def pick_interface(counters: dict[str, tuple[int, int]], default_iface: str | None) -> str | None:
    """The default-route interface, else the real interface with the most traffic."""
    if default_iface and default_iface in counters and not is_virtual(default_iface):
        return default_iface
    real = {k: v for k, v in counters.items() if not is_virtual(k)}
    if not real:
        return None
    return max(real, key=lambda k: (real[k][0] + real[k][1], k))


def counter_delta(prev: int, cur: int) -> int | None:
    """Difference of a monotonically growing counter; None after a reset."""
    if cur >= prev:
        return cur - prev
    if WRAP_32 * 0.9 <= prev < WRAP_32 and cur < WRAP_32 * 0.1:
        return cur + WRAP_32 - prev  # 32-bit counter wrapped
    return None


# ---------------------------------------------------------------------------------- service


class ServerMetricsService:
    def __init__(
        self,
        ops: SystemOps,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disk_paths: tuple[str, ...] = DISK_PATHS,
    ) -> None:
        self._ops = ops
        self._mono = monotonic
        self._sleep = sleep
        self._disk_paths = disk_paths
        self._lock = asyncio.Lock()
        self._cache: tuple[float, MetricsView] | None = None
        self._cpu: tuple[int, int] | None = None
        self._net: tuple[float, str, int, int] | None = None  # (time, iface, rx, tx)

    async def _read(self, path: str) -> str | None:
        try:
            return (await self._ops.read_file(path)).decode("utf-8", "replace")
        except SystemOpsError:
            return None

    async def snapshot(self) -> MetricsView:
        async with self._lock:
            now = self._mono()
            if self._cache is not None and 0 <= now - self._cache[0] < CACHE_S:
                return self._cache[1]
            first = self._cpu is None and self._net is None
            view = await self._sample()
            if first:  # no earlier sample to compare with: take a second one right away
                await self._sleep(BASELINE_WAIT_S)
                view = await self._sample()
            self._cache = (self._mono(), view)
            return view

    async def _sample(self) -> MetricsView:
        errors: list[str] = []
        stat = await self._read("/proc/stat")
        cpu_pct: float | None = None
        cores: int | None = None
        parsed_cpu = parse_cpu(stat) if stat else None
        if parsed_cpu is None:
            errors.append("cpu")
        else:
            busy, total, cores = parsed_cpu
            if self._cpu is not None:
                d_total, d_busy = total - self._cpu[1], busy - self._cpu[0]
                if d_total > 0 and d_busy >= 0:
                    cpu_pct = min(100.0, 100.0 * d_busy / d_total)
            self._cpu = (busy, total)

        mem_text = await self._read("/proc/meminfo")
        mem = parse_meminfo(mem_text) if mem_text else None
        if mem is None:
            errors.append("memory")
        load_text = await self._read("/proc/loadavg")
        up_text = await self._read("/proc/uptime")

        rx_mbit = tx_mbit = None
        iface: str | None = None
        net_text = await self._read("/proc/net/dev")
        if net_text is None:
            errors.append("network")
        else:
            route_text = await self._read("/proc/net/route")
            counters = parse_netdev(net_text)
            iface = pick_interface(counters, parse_default_iface(route_text or ""))
            if iface is not None:
                rx, tx = counters[iface]
                now = self._mono()
                prev = self._net
                if prev is not None and prev[1] == iface and now > prev[0]:
                    d_rx, d_tx = counter_delta(prev[2], rx), counter_delta(prev[3], tx)
                    dt = now - prev[0]
                    if d_rx is not None and d_tx is not None:
                        rx_mbit, tx_mbit = d_rx * 8 / dt / 1e6, d_tx * 8 / dt / 1e6
                self._net = (now, iface, rx, tx)
            else:
                self._net = None

        disks: list[DiskView] = []
        seen: set[tuple[int, int]] = set()
        for path in self._disk_paths:
            try:
                usage = await self._ops.disk_usage(path)
            except SystemOpsError:
                continue
            key = (usage.total, usage.used)
            if key in seen:  # same filesystem as an earlier path
                continue
            seen.add(key)
            disks.append(DiskView(path, usage.used, usage.total))
        if not disks:
            errors.append("disk")

        return MetricsView(
            cpu_pct=cpu_pct,
            cores=cores or None,
            load=parse_loadavg(load_text) if load_text else None,
            mem_used=None if mem is None else mem[0],
            mem_total=None if mem is None else mem[1],
            disks=tuple(disks),
            net_iface=iface,
            rx_mbit=rx_mbit,
            tx_mbit=tx_mbit,
            uptime_s=parse_uptime(up_text) if up_text else None,
            errors=tuple(errors),
        )
