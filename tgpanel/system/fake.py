"""FakeSystemOps: fully in-memory SystemOps for tests (no OS access at all).

Features: dict filesystem with mode/owner/group, tar archives stored in the fs, a small
systemd state machine, nft sets with counters, a port-open registry, configurable HTTP/DNS/
cert/public-IP answers, a faithful-enough `tproxy_check`, real asyncio lock semantics, an
ordered call log and failure injection (`fail_on`, `fail_check`).

Unit names are normalised: `foo` and `foo.service` are the same unit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import posixpath
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from tgpanel.system.ops import (
    CertInfo,
    CheckResult,
    DiskUsage,
    FileStat,
    HttpResult,
    LocalFile,
    LockHandle,
    NftTableMissing,
    SetCounter,
    SystemOpsError,
)
from tgpanel.system.validation import (
    build_tar_gz,
    extract_tar_member_to_file,
    iter_tar_members,
    normalize_unit,
    parse_tar_gz,
    validate_env,
    validate_ipv4,
    validate_nft_ident,
    validate_path,
    validate_property,
    validate_systemctl,
    validate_unit,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "upstream"
_SECRET_RE = re.compile(r"^(?:dd)?[0-9a-fA-F]{32}$|^[A-Za-z0-9_-]{22,}$")
_PROFILE_KEYS = {"name", "secret", "backend", "carrier_mode", "limits"}
_CARRIER_MODES = {"https", "https-lanes", "websocket", "websocket-lanes"}
_POOL_UNIT_RE = re.compile(r"^tgpanel-mtproxy@([A-Za-z0-9_.-]+?)(?:\.service)?$")
_ENV_PORT_RE = re.compile(r"^MTP_PORT=\"?(\d+)\"?\s*$", re.MULTILINE)

Matcher = str | Callable[[tuple[Any, ...]], bool] | None


@dataclass(slots=True)
class FakeFile:
    data: bytes
    mode: int = 0o644
    owner: str = "root"
    group: str = "root"


@dataclass(slots=True)
class _Injection:
    method: str
    match: Matcher
    times: int | None
    skip: int
    exc: type[Exception] | Exception
    check_output: str | None = None  # set => returns CheckResult(False, ...) instead of raising


class FakeLockHandle:
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock: asyncio.Lock | None = lock

    async def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()


class FakeSystemOps:
    def __init__(self, *, strict_dirs: bool = False) -> None:
        # files / dirs
        self.files: dict[str, FakeFile] = {}
        self.dirs: set[str] = {"/"}
        self.disks: dict[str, DiskUsage] = {"/": DiskUsage(100 * 2**30, 40 * 2**30)}
        self.strict_dirs = strict_dirs
        # call log: (method, *args). Secrets-bearing payloads (file data) are never logged.
        self.calls: list[tuple[Any, ...]] = []
        self._injections: list[_Injection] = []
        # systemd
        self.active: set[str] = set()
        self.enabled: set[str] = set()
        self.masked: set[str] = set()
        self.restart_counts: dict[str, int] = {}
        self.daemon_reloads = 0
        self.unit_props: dict[tuple[str, str], str] = {}
        # ports
        self.open_ports: set[int] = set()  # manual open
        self.forced_closed_ports: set[int] = set()  # manual override: never open
        self.unit_ports: dict[str, tuple[int, ...]] = {
            "tproxy-server": (8080, 8081),
            "caddy": (80, 443),
            "mtproxy": (2398,),
        }
        self.auto_pool_ports = True  # active tgpanel-mtproxy@N + its env file => MTP_PORT open
        # http
        self.http_responses: dict[str, HttpResult] = {}
        self.http_script: dict[str, list[HttpResult]] = {}
        # dns / ip / tls
        self.dns: dict[str, tuple[list[str], list[str]]] = {}
        self.public_ip: str | None = "203.0.113.10"
        self.certs: dict[str, CertInfo | None] = {}
        self.cert_script: dict[str, list[CertInfo | None]] = {}
        # nft
        self.nft_sets: dict[tuple[str, str], dict[str, SetCounter]] = {
            ("tgpanel", "up"): {},
            ("tgpanel", "down"): {},
        }
        self.nft_loaded: list[str] = []  # contents of every nft_load_file
        # checks
        self.invalid_caddyfiles: set[str] = set()
        self.last_caddy_env: dict[str, str] | None = None
        # locks
        self._locks: dict[str, asyncio.Lock] = {}

    # ===================================================================================
    # test helpers
    # ===================================================================================

    def put_file(
        self,
        path: str,
        data: bytes | str,
        *,
        mode: int = 0o644,
        owner: str = "root",
        group: str = "root",
    ) -> None:
        """Place a file directly (not logged, not subject to injection)."""
        validate_path(path)
        raw = data.encode() if isinstance(data, str) else data
        self.files[path] = FakeFile(raw, mode, owner, group)
        self.mkdir(posixpath.dirname(path))

    def mkdir(self, path: str) -> None:
        while path and path != "/" and path not in self.dirs:
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def get_text(self, path: str) -> str:
        return self.files[path].data.decode()

    def get_json(self, path: str) -> Any:
        return json.loads(self.files[path].data)

    def calls_of(self, method: str, match: str | None = None) -> list[tuple[Any, ...]]:
        """Calls to `method` whose space-joined args contain `match` (if given)."""
        return [
            c
            for c in self.calls
            if c[0] == method and (match is None or match in _join_args(c[1:]))
        ]

    def call_count(self, method: str, match: str | None = None) -> int:
        return len(self.calls_of(method, match))

    def systemctl_calls(
        self, action: str | None = None, unit: str | None = None
    ) -> list[tuple[str, str]]:
        """[(action, normalised unit)] in call order, optionally filtered."""
        out = [(c[1], normalize_unit(c[2])) for c in self.calls if c[0] == "systemctl"]
        return [
            (a, u)
            for a, u in out
            if (action is None or a == action) and (unit is None or u == normalize_unit(unit))
        ]

    def clear_calls(self) -> None:
        self.calls.clear()

    def restart_count(self, unit: str) -> int:
        return self.restart_counts.get(normalize_unit(unit), 0)

    def set_active(self, unit: str, active: bool = True) -> None:
        (self.active.add if active else self.active.discard)(normalize_unit(unit))

    def set_port_open(self, port: int, is_open: bool = True) -> None:
        """Manual override: True => open regardless of units; False => forced closed."""
        self.open_ports.discard(port)
        self.forced_closed_ports.discard(port)
        (self.open_ports if is_open else self.forced_closed_ports).add(port)

    def set_http(self, url: str, status: int = 200, body: bytes = b"") -> None:
        """Answer for an exact URL, or for any URL ending with `url` (e.g. "/readyz")."""
        self.http_responses[url] = HttpResult(status, body)

    def queue_http(self, url: str, results: list[HttpResult]) -> None:
        """One-shot answers consumed (in order) before the configured/default answer."""
        self.http_script.setdefault(url, []).extend(results)

    def set_dns(
        self, hostname: str, a: list[str] | None = None, aaaa: list[str] | None = None
    ) -> None:
        self.dns[hostname] = (list(a or []), list(aaaa or []))

    def set_cert(self, hostname: str, cert: CertInfo | None) -> None:
        self.certs[hostname] = cert

    def queue_cert(self, hostname: str, results: list[CertInfo | None]) -> None:
        self.cert_script.setdefault(hostname, []).extend(results)

    def mark_caddyfile_invalid(self, path: str) -> None:
        self.invalid_caddyfiles.add(path)

    def ensure_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        return self.nft_sets.setdefault((table, set_name), {})

    def add_traffic(
        self,
        ip: str,
        *,
        up_bytes: int = 0,
        down_bytes: int = 0,
        up_packets: int = 0,
        down_packets: int = 0,
        table: str = "tgpanel",
        create: bool = False,
    ) -> None:
        """Increase counters of `ip` in sets up/down. Element must exist unless create=True."""
        for name, nbytes, npk in (("up", up_bytes, up_packets), ("down", down_bytes, down_packets)):
            if not (nbytes or npk):
                continue
            s = self.nft_sets[(table, name)]
            if ip not in s:
                if not create:
                    raise ValueError(f"{ip} is not an element of {table}/{name}")
                s[ip] = SetCounter(0, 0)
            cur = s[ip]
            s[ip] = SetCounter(cur.bytes + nbytes, cur.packets + npk)

    def seed_upstream(self, variant: Literal["clean", "owner"]) -> None:
        """Load tests/fixtures/upstream/<variant> at the real /etc paths (PLAN 2.2/2.7)."""
        base = _FIXTURES / variant
        if not base.is_dir():
            raise FileNotFoundError(base)
        placement: dict[str, tuple[str, int, str, str]] = {
            "config.json": ("/etc/tproxy-server/config.json", 0o640, "root", "tproxy"),
            "profiles.json": ("/etc/tproxy-server/profiles.json", 0o400, "root", "tproxy"),
            "mtproxy.service": ("/etc/systemd/system/mtproxy.service", 0o644, "root", "root"),
            "mtproxy.env": ("/etc/mtproxy/mtproxy.env", 0o640, "root", "mtproxy"),
            "mtproxy.secrets": ("/etc/mtproxy/mtproxy.secrets", 0o640, "root", "mtproxy"),
            "Caddyfile": ("/etc/caddy/Caddyfile", 0o644, "root", "root"),
            "caddy.service.d/tproxy.conf": (
                "/etc/systemd/system/caddy.service.d/tproxy.conf",
                0o644,
                "root",
                "root",
            ),
            "mtproxy.service.d/nat.conf": (
                "/etc/systemd/system/mtproxy.service.d/nat.conf",
                0o644,
                "root",
                "root",
            ),
        }
        for rel, (dest, mode, owner, group) in placement.items():
            src = base / rel
            if src.is_file():
                self.put_file(dest, src.read_bytes(), mode=mode, owner=owner, group=group)
        self.put_file("/etc/mtproxy/proxy-secret", b"\x00" * 16, mode=0o640, group="mtproxy")
        self.put_file(
            "/etc/mtproxy/proxy-multi.conf", b"# synthetic\n", mode=0o640, group="mtproxy"
        )
        for d in ("/etc/tgpanel", "/etc/tgpanel/mtproxy", "/var/backups/tgpanel", "/run/tgpanel"):
            self.mkdir(d)
        for unit in ("tproxy-server", "mtproxy", "caddy"):
            self.active.add(unit)
            self.enabled.add(unit)

    # ===================================================================================
    # failure injection
    # ===================================================================================

    def fail_on(
        self,
        method: str,
        match: Matcher = None,
        times: int | None = 1,
        exc: type[Exception] | Exception = SystemOpsError,
        *,
        skip: int = 0,
    ) -> None:
        """Make calls to `method` raise `exc`.

        `match`: substring of the space-joined call args (e.g. "restart tproxy-server" for
        systemctl, a path for write_atomic) or a predicate over the args tuple. `skip`: let the
        first N matching calls through (Nth-call failures). `times=None`: fail forever.
        """
        self._injections.append(_Injection(method, match, times, skip, exc))

    def fail_check(
        self,
        method: Literal["tproxy_check", "caddy_validate", "nft_check_file"],
        output: str = "injected check failure",
        match: Matcher = None,
        times: int | None = 1,
        *,
        skip: int = 0,
    ) -> None:
        """Make a check return CheckResult(ok=False, output=...) instead of raising."""
        self._injections.append(_Injection(method, match, times, skip, SystemOpsError, output))

    def clear_failures(self) -> None:
        self._injections.clear()

    def _enter(self, method: str, *args: Any) -> CheckResult | None:
        """Log the call; raise (or return a failing CheckResult) when an injection matches."""
        self.calls.append((method, *args))
        for inj in list(self._injections):
            if inj.method != method:
                continue
            if inj.match is not None:
                hit = inj.match(args) if callable(inj.match) else inj.match in _join_args(args)
                if not hit:
                    continue
            if inj.skip > 0:
                inj.skip -= 1
                continue
            if inj.times is not None:
                inj.times -= 1
                if inj.times <= 0:
                    self._injections.remove(inj)
            if inj.check_output is not None:
                return CheckResult(False, inj.check_output)
            if isinstance(inj.exc, Exception):
                raise inj.exc
            raise inj.exc(f"injected failure: {method}")
        return None

    # ===================================================================================
    # files
    # ===================================================================================

    async def read_file(self, path: str) -> bytes:
        validate_path(path)
        self._enter("read_file", path)
        return self._get(path).data

    def _get(self, path: str) -> FakeFile:
        f = self.files.get(path)
        if f is None:
            raise SystemOpsError("file operation failed: No such file or directory")
        return f

    async def exists(self, path: str) -> bool:
        validate_path(path)
        self._enter("exists", path)
        return path in self.files or path in self.dirs

    async def stat(self, path: str) -> FileStat:
        validate_path(path)
        self._enter("stat", path)
        f = self._get(path)
        return FileStat(f.mode, f.owner, f.group, len(f.data))

    async def disk_usage(self, path: str) -> DiskUsage:
        validate_path(path)
        self._enter("disk_usage", path)
        best = ""
        for mount in self.disks:  # longest matching mount point wins
            if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) > len(
                best
            ):
                best = mount
        if not best:
            raise SystemOpsError("disk usage failed: no such mount")
        return self.disks[best]

    async def list_dir(self, path: str) -> list[str]:
        validate_path(path)
        self._enter("list_dir", path)
        if path not in self.dirs:
            raise SystemOpsError("file operation failed: No such file or directory")
        names = {
            p[len(path.rstrip("/")) + 1 :].split("/", 1)[0]
            for p in (*self.files, *self.dirs)
            if p != path and p.startswith(path.rstrip("/") + "/")
        }
        return sorted(names)

    async def write_atomic(
        self, path: str, data: bytes, *, mode: int, owner: str, group: str
    ) -> None:
        validate_path(path)
        self._enter("write_atomic", path, mode, owner, group)
        if not 0 <= mode <= 0o7777:
            raise SystemOpsError("invalid file mode")
        parent = posixpath.dirname(path)
        if self.strict_dirs and parent not in self.dirs:
            raise SystemOpsError("file operation failed: No such file or directory")
        self.put_file(path, data, mode=mode, owner=owner, group=group)

    async def remove(self, path: str) -> None:
        validate_path(path)
        self._enter("remove", path)
        self.files.pop(path, None)

    async def make_tar_gz(self, dest: str, members: Mapping[str, bytes | LocalFile]) -> None:
        validate_path(dest)
        self._enter("make_tar_gz", dest, tuple(sorted(members)))
        blob = build_tar_gz(members)  # LocalFile members are streamed from their local path
        self.put_file(dest, blob, mode=0o600)

    async def read_tar_gz(self, path: str) -> dict[str, bytes]:
        validate_path(path)
        self._enter("read_tar_gz", path)
        return parse_tar_gz(self._get(path).data)

    async def read_tar_members(
        self, path: str, names: Collection[str] | None = None
    ) -> dict[str, bytes]:
        validate_path(path)
        self._enter("read_tar_members", path, tuple(sorted(names)) if names is not None else None)
        return iter_tar_members(self._get(path).data, names)

    async def extract_tar_member(self, path: str, name: str, dest: str) -> bool:
        validate_path(path)
        self._enter("extract_tar_member", path, name)
        return extract_tar_member_to_file(self._get(path).data, name, dest)

    async def ensure_dir(self, path: str, mode: int, owner: str, group: str) -> None:
        validate_path(path)
        self._enter("ensure_dir", path, mode, owner, group)
        self.mkdir(path)

    # ===================================================================================
    # systemd
    # ===================================================================================

    async def systemctl(self, action: str, unit: str) -> None:
        validate_systemctl(action, unit)
        self._enter("systemctl", action, unit)
        if action == "daemon-reload":
            self.daemon_reloads += 1
            return
        u = normalize_unit(unit)
        if action in ("start", "restart", "enable-now") and u in self.masked:
            raise SystemOpsError(f"systemctl {action} {unit} failed: unit is masked")
        if action in ("start", "enable-now"):
            self.active.add(u)
        elif action == "restart":
            self.active.add(u)
            self.restart_counts[u] = self.restart_counts.get(u, 0) + 1
        elif action == "stop":
            self.active.discard(u)
        elif action == "disable-now":
            self.active.discard(u)
        elif action == "mask-now":
            self.active.discard(u)
            self.masked.add(u)
        elif action == "unmask":
            self.masked.discard(u)
        if action in ("enable", "enable-now"):
            if u in self.masked:
                raise SystemOpsError(f"systemctl {action} {unit} failed: unit is masked")
            self.enabled.add(u)
        elif action in ("disable", "disable-now", "mask-now"):
            self.enabled.discard(u)

    async def is_active(self, unit: str) -> bool:
        validate_unit(unit)
        self._enter("is_active", unit)
        return normalize_unit(unit) in self.active

    async def unit_property(self, unit: str, prop: str) -> str:
        validate_unit(unit)
        validate_property(prop)
        self._enter("unit_property", unit, prop)
        u = normalize_unit(unit)
        if (u, prop) in self.unit_props:
            return self.unit_props[(u, prop)]
        if prop == "ActiveState":
            return "active" if u in self.active else "inactive"
        if prop == "SubState":
            return "running" if u in self.active else "dead"
        if prop == "NRestarts":
            return str(self.restart_counts.get(u, 0))
        if prop == "UnitFileState":
            return "masked" if u in self.masked else "enabled" if u in self.enabled else "disabled"
        return ""

    # ===================================================================================
    # network / health
    # ===================================================================================

    def port_is_open(self, port: int) -> bool:
        if port in self.forced_closed_ports:
            return False
        if port in self.open_ports:
            return True
        for unit in self.active:
            if port in self.unit_ports.get(unit, ()):
                return True
            m = _POOL_UNIT_RE.match(unit)
            if m and self.auto_pool_ports:
                env = self.files.get(f"/etc/tgpanel/mtproxy/{m.group(1)}.env")
                if env is not None:
                    pm = _ENV_PORT_RE.search(env.data.decode(errors="replace"))
                    if pm and int(pm.group(1)) == port:
                        return True
        return False

    async def wait_tcp_open(self, host: str, port: int, timeout_s: float) -> bool:
        self._enter("wait_tcp_open", host, port)
        await asyncio.sleep(0)
        return self.port_is_open(port)

    async def http_get(self, url: str, timeout_s: float) -> HttpResult:
        self._enter("http_get", url)
        await asyncio.sleep(0)
        for key in (url, *[k for k in self.http_script if url.endswith(k)]):
            queue = self.http_script.get(key)
            if queue:
                return queue.pop(0)
        if url in self.http_responses:
            return self.http_responses[url]
        for key, res in self.http_responses.items():
            if url.endswith(key):
                return res
        if url.endswith("/metrics"):
            return HttpResult(200, b"tproxy_sessions_live 0\ntproxy_streams_live 0\n")
        if url.endswith(("/healthz", "/readyz")):
            return HttpResult(200, b"ok\n")
        return HttpResult(404, b"not found")

    async def resolve(self, hostname: str) -> tuple[list[str], list[str]]:
        self._enter("resolve", hostname)
        a, aaaa = self.dns.get(hostname, ([], []))
        return list(a), list(aaaa)

    async def public_ipv4(self) -> str | None:
        self._enter("public_ipv4")
        return self.public_ip

    async def tls_cert_info(self, hostname: str) -> CertInfo | None:
        self._enter("tls_cert_info", hostname)
        queue = self.cert_script.get(hostname)
        if queue:
            return queue.pop(0)
        return self.certs.get(hostname)

    # ===================================================================================
    # proxy tooling
    # ===================================================================================

    async def tproxy_check(self, config_path: str, profiles_path: str) -> CheckResult:
        validate_path(config_path)
        validate_path(profiles_path)
        injected = self._enter("tproxy_check", config_path, profiles_path)
        if injected is not None:
            return injected
        problems = self._check_tproxy(config_path, profiles_path)
        if problems:
            return CheckResult(False, "; ".join(problems[:10]))
        return CheckResult(True, "ok")

    def _check_tproxy(self, config_path: str, profiles_path: str) -> list[str]:
        cfg_file, prof_file = self.files.get(config_path), self.files.get(profiles_path)
        if cfg_file is None:
            return ["config: no such file"]
        if prof_file is None:
            return ["profiles: no such file"]
        try:
            cfg = json.loads(cfg_file.data)
        except ValueError:
            return ["config: invalid JSON"]
        if not isinstance(cfg, dict):
            return ["config: must be a JSON object"]
        max_profiles = 32
        limits = cfg.get("limits")
        if limits is not None:
            if not isinstance(limits, dict):
                return ["config: limits must be an object"]
            for k, v in limits.items():
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    return [f"config: limits.{k} must be a non-negative integer"]
            if "max_profiles" in limits:
                max_profiles = limits["max_profiles"]
                if max_profiles < 1:
                    return ["config: limits.max_profiles must be >= 1"]
        problems: list[str] = []
        if prof_file.mode & 0o077:  # the real binary refuses group/other access, even read
            problems.append(f"profiles: file mode {prof_file.mode:04o} too permissive")
        try:
            doc = json.loads(prof_file.data)
        except ValueError:
            return [*problems, "profiles: invalid JSON"]
        if not isinstance(doc, dict) or set(doc) != {"profiles"}:
            return [*problems, 'profiles: top level must be exactly {"profiles": [...]}']
        profiles = doc["profiles"]
        if not isinstance(profiles, list):
            return [*problems, "profiles: must be a list"]
        if not profiles:
            problems.append("profiles: no profiles configured")
        if len(profiles) > max_profiles:
            problems.append(f"profiles: {len(profiles)} exceeds limits.max_profiles={max_profiles}")
        seen: set[str] = set()
        for i, p in enumerate(profiles):
            problems.extend(_check_profile(i, p, seen))
        return problems

    async def caddy_validate(self, caddyfile_path: str, env: dict[str, str]) -> CheckResult:
        validate_path(caddyfile_path)
        validate_env(env)
        injected = self._enter("caddy_validate", caddyfile_path)
        self.last_caddy_env = dict(env)
        if injected is not None:
            return injected
        f = self.files.get(caddyfile_path)
        if f is None:
            return CheckResult(False, "no such file")
        text = f.data.decode(errors="replace")
        if caddyfile_path in self.invalid_caddyfiles or "# fake:invalid" in text:
            return CheckResult(False, "flagged invalid")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        depth = 0
        for ch in code:
            depth += (ch == "{") - (ch == "}")
            if depth < 0:
                break
        if depth != 0:
            return CheckResult(False, "unbalanced braces")
        return CheckResult(True, "Valid configuration")

    # ===================================================================================
    # nftables
    # ===================================================================================

    async def nft_load_file(self, path: str) -> None:
        validate_path(path)
        self._enter("nft_load_file", path)
        text = self._get(path).data.decode(errors="replace")
        self.nft_loaded.append(text)
        for tm in re.finditer(r"table\s+inet\s+(\w+)\s*\{(.*?)^\}", text, re.S | re.M):
            for sm in re.finditer(r"^\s*set\s+(\w+)\s*\{", tm.group(2), re.M):
                self.nft_sets.setdefault((tm.group(1), sm.group(1)), {})

    def _set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        validate_nft_ident(table, "table")
        validate_nft_ident(set_name, "set")
        s = self.nft_sets.get((table, set_name))
        if s is None:
            raise NftTableMissing("nft list set failed: No such file or directory")
        return s

    async def nft_check_file(self, path: str) -> CheckResult:
        validate_path(path)
        injected = self._enter("nft_check_file", path)
        if injected is not None:
            return injected
        if path not in self.files:
            return CheckResult(False, "no such file")
        return CheckResult(True, "ok")

    async def nft_list_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        self._enter("nft_list_set", table, set_name)
        return dict(self._set(table, set_name))

    async def nft_delete_table(self, table: str) -> None:
        validate_nft_ident(table, "table")
        self._enter("nft_delete_table", table)
        for key in [k for k in self.nft_sets if k[0] == table]:
            del self.nft_sets[key]

    async def nft_add_elements(self, table: str, set_name: str, ips: list[str]) -> None:
        self._enter("nft_add_elements", table, set_name, tuple(ips))
        s = self._set(table, set_name)
        for ip in [validate_ipv4(i) for i in ips]:
            s.setdefault(ip, SetCounter(0, 0))

    async def nft_delete_elements(self, table: str, set_name: str, ips: list[str]) -> None:
        self._enter("nft_delete_elements", table, set_name, tuple(ips))
        s = self._set(table, set_name)
        clean = [validate_ipv4(i) for i in ips]
        missing = [ip for ip in clean if ip not in s]
        if missing:
            raise SystemOpsError("nft delete element failed: No such file or directory")
        for ip in clean:
            del s[ip]

    # ===================================================================================
    # locking
    # ===================================================================================

    async def acquire_lock(self, path: str, timeout_s: float) -> LockHandle:
        validate_path(path)
        self._enter("acquire_lock", path)
        lock = self._locks.setdefault(path, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout_s)
        except TimeoutError:
            raise SystemOpsError("timed out waiting for lock") from None
        return FakeLockHandle(lock)

    def is_locked(self, path: str) -> bool:
        lock = self._locks.get(path)
        return lock is not None and lock.locked()


def _join_args(args: tuple[Any, ...]) -> str:
    return " ".join(str(a) for a in args)


def _check_profile(i: int, p: Any, seen: set[str]) -> list[str]:
    tag = f"profiles[{i}]"
    if not isinstance(p, dict):
        return [f"{tag}: must be an object"]
    out: list[str] = []
    unknown = sorted(set(p) - _PROFILE_KEYS)
    if unknown:
        out.append(f"{tag}: unknown field(s) {', '.join(map(str, unknown))[:80]}")
    name = p.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        out.append(f"{tag}: name must be 1-64 characters")
    elif name in seen:
        out.append(f"{tag}: duplicate name")
    else:
        seen.add(name)
    secret = p.get("secret")
    if not isinstance(secret, str) or not _SECRET_RE.match(secret):
        out.append(f"{tag}: bad secret format")
    backend = p.get("backend")
    if not isinstance(backend, str) or ":" not in backend:
        out.append(f"{tag}: backend must be host:port")
    else:
        host, _, port = backend.rpartition(":")
        try:
            ok_host = ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            ok_host = False
        if not ok_host:
            out.append(f"{tag}: backend must be a numeric loopback address")
        if not port.isdigit() or not 0 < int(port) < 65536:
            out.append(f"{tag}: backend port invalid")
    mode = p.get("carrier_mode", "https")
    if mode not in _CARRIER_MODES:
        out.append(f"{tag}: invalid carrier_mode")
    lim = p.get("limits")
    if lim is not None and not isinstance(lim, dict):
        out.append(f"{tag}: limits must be an object")
    return out
