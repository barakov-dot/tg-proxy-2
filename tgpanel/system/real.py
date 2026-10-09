"""RealSystemOps: the production SystemOps. Linux/root only for the OS-touching parts.

Rules: external commands only as argument lists via asyncio.create_subprocess_exec (never a
shell); every value that reaches a command line is validated first; command output that
leaves this module is scrubbed of secrets and truncated; blocking work runs off the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import grp
import http.client
import ipaddress
import json
import os
import pwd
import signal
import socket
import ssl
import stat as stat_mod
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tgpanel.system.ops import (
    CertInfo,
    CheckResult,
    FileStat,
    HttpResult,
    LocalFile,
    LockHandle,
    NftTableMissing,
    SetCounter,
    SystemOpsError,
)
from tgpanel.system.validation import (
    extract_tar_member_to_file,
    iter_tar_members,
    parse_tar_gz,
    scrub,
    validate_env,
    validate_ipv4,
    validate_nft_ident,
    validate_path,
    validate_property,
    validate_systemctl,
    validate_unit,
    write_tar_gz,
)

_MINIMAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_MAX_HTTP_BODY = 8 * 1024 * 1024
CADDY_VALIDATE_DIR = "/run/tgpanel/caddy-validate"  # throwaway XDG dirs for `caddy validate`
_KILL_GRACE_S = 5.0  # how long to wait for the pipes of a killed command to close
_DEFAULT_IP_PROBES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ipv4.icanhazip.com",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Scrubbed, truncated text of stdout+stderr; safe to show or log."""
        text = (self.stdout + self.stderr).decode("utf-8", "replace").strip()
        if self.timed_out:
            text = f"timed out; {text}"
        return scrub(text)


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": _MINIMAL_PATH, "LC_ALL": "C", "LANG": "C"}
    if extra:
        env.update(extra)
    return env


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the command and everything it started (it runs in its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    cwd: str | None = None,
) -> CommandResult:
    """Run an argument list (no shell) with a minimal environment and a timeout."""
    if not argv or any("\x00" in a for a in argv):
        raise SystemOpsError("invalid command line")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(env),
            cwd=cwd,
            start_new_session=True,  # own process group: a timeout kills grandchildren too
        )
    except OSError as exc:
        raise SystemOpsError(
            f"cannot execute {os.path.basename(argv[0])}: {exc.strerror}"
        ) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        _kill_group(proc)
        try:  # a killed child can still hold the pipes open (grandchildren): bound the wait
            out, err = await asyncio.wait_for(proc.communicate(), _KILL_GRACE_S)
        except (TimeoutError, OSError):
            out, err = b"", b""
        return CommandResult(-1, out, err, timed_out=True)
    except BaseException:
        _kill_group(proc)
        await proc.wait()
        raise
    return CommandResult(proc.returncode if proc.returncode is not None else -1, out, err)


# --- sync file helpers (run in threads) ---------------------------------------------------


def _resolve_uid(owner: str) -> int:
    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        raise SystemOpsError("unknown owner") from None


def _resolve_gid(group: str) -> int:
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        raise SystemOpsError("unknown group") from None


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic_sync(path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    """Temp file in the same dir (created 0600), chown+chmod BEFORE rename, fsync, rename."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tgpanel-", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o600)
            if uid != os.geteuid() or gid != os.getegid():
                os.fchown(fd, uid, gid)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def write_tar_stream_sync(dest: str, members: Mapping[str, bytes | LocalFile]) -> None:
    """Stream a 0600 tar.gz to a temp file next to ``dest``, fsync, rename."""
    directory = os.path.dirname(dest)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tgpanel-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            write_tar_gz(out, members)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def parse_nft_set_json(raw: bytes | str) -> dict[str, SetCounter]:
    """Parse `nft -j list set ...` output into ip -> SetCounter. Elements w/o counters give 0."""
    try:
        doc = json.loads(raw)
    except ValueError:
        raise SystemOpsError("nft returned invalid JSON") from None
    result: dict[str, SetCounter] = {}
    items: Any = doc.get("nftables") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise SystemOpsError("unexpected nft JSON structure")
    for item in items:
        nft_set = item.get("set") if isinstance(item, dict) else None
        if not isinstance(nft_set, dict):
            continue
        for elem in nft_set.get("elem") or []:
            counter: dict[str, Any] = {}
            val: Any = elem
            if isinstance(elem, dict) and "elem" in elem:
                inner = elem["elem"]
                val = inner.get("val") if isinstance(inner, dict) else None
                c = inner.get("counter") if isinstance(inner, dict) else None
                if isinstance(c, dict):
                    counter = c
            if not isinstance(val, str):
                continue  # prefixes / ranges are not per-user entries
            try:
                ip = str(ipaddress.IPv4Address(val))
            except ValueError:
                continue
            result[ip] = SetCounter(
                bytes=int(counter.get("bytes", 0)), packets=int(counter.get("packets", 0))
            )
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _http_get_sync(url: str, timeout_s: float) -> HttpResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SystemOpsError("only http/https URLs are allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return HttpResult(status=resp.status, body=resp.read(_MAX_HTTP_BODY))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_MAX_HTTP_BODY)
        except OSError:
            body = b""
        return HttpResult(status=exc.code, body=body)
    except (OSError, http.client.HTTPException, ValueError):
        return HttpResult(status=0)


def _cert_info_from_dict(cert: dict[str, Any], valid_chain: bool) -> CertInfo:
    names: dict[str, str] = {}
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            names[key] = value
    issuer = names.get("organizationName") or names.get("commonName") or ""

    def iso(value: str) -> str:
        ts = ssl.cert_time_to_seconds(value)
        return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return CertInfo(
        issuer=issuer,
        not_before=iso(cert["notBefore"]),
        not_after=iso(cert["notAfter"]),
        valid_chain=valid_chain,
    )


def _name_matches(pattern: str, hostname: str) -> bool:
    pattern, hostname = pattern.lower(), hostname.lower()
    if pattern.startswith("*."):
        head, _, rest = hostname.partition(".")
        return bool(head) and rest == pattern[2:]
    return pattern == hostname


def _cert_covers_host(cert: dict[str, Any], hostname: str) -> bool:
    sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    if not sans:
        for rdn in cert.get("subject", ()):
            sans += [v for k, v in rdn if k == "commonName"]
    return any(_name_matches(name, hostname) for name in sans)


def _tls_cert_info_sync(hostname: str, port: int, timeout_s: float) -> CertInfo | None:
    """Public-API only: the chain is verified by the handshake (CERT_REQUIRED); the host name
    is matched against the verified certificate by hand."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        with (
            socket.create_connection((hostname, port), timeout=timeout_s) as sock,
            ctx.wrap_socket(sock, server_hostname=hostname) as tls,
        ):
            cert = tls.getpeercert()
    except ssl.SSLCertVerificationError:
        return CertInfo(issuer="", not_before="", not_after="", valid_chain=False)
    except (OSError, ssl.SSLError, ValueError):
        return None
    if not cert:
        return None
    try:
        return _cert_info_from_dict(dict(cert), _cert_covers_host(dict(cert), hostname))
    except (KeyError, ValueError):
        return None


class RealLockHandle:
    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    async def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _check_hostname(hostname: str) -> None:
    if not hostname or len(hostname) > 253 or any(c.isspace() or c == "/" for c in hostname):
        raise SystemOpsError("invalid hostname")


async def _fs[T](fn: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(fn)
    except OSError as exc:
        raise SystemOpsError(f"file operation failed: {exc.strerror or 'error'}") from None


class RealSystemOps:
    def __init__(
        self,
        *,
        systemctl_bin: str = "systemctl",
        nft_bin: str = "nft",
        tproxy_bin: str = "/usr/local/bin/tproxy-server",
        caddy_bin: str = "/usr/local/bin/caddy",
        ip_probe_urls: Sequence[str] = _DEFAULT_IP_PROBES,
        command_timeout_s: float = 30.0,
        tls_port: int = 443,
    ) -> None:
        self._systemctl = systemctl_bin
        self._nft = nft_bin
        self._tproxy = tproxy_bin
        self._caddy = caddy_bin
        self._ip_probes = tuple(ip_probe_urls)
        self._timeout = command_timeout_s
        self._tls_port = tls_port

    # --- files ---------------------------------------------------------------------------

    async def read_file(self, path: str) -> bytes:
        validate_path(path)

        def _read() -> bytes:
            with open(path, "rb") as f:
                return f.read()

        return await _fs(_read)

    async def exists(self, path: str) -> bool:
        validate_path(path)
        return await asyncio.to_thread(os.path.exists, path)

    async def stat(self, path: str) -> FileStat:
        validate_path(path)

        def _stat() -> FileStat:
            st = os.stat(path)
            return FileStat(
                mode=stat_mod.S_IMODE(st.st_mode),
                owner=_owner_name(st.st_uid),
                group=_group_name(st.st_gid),
                size=st.st_size,
            )

        return await _fs(_stat)

    async def list_dir(self, path: str) -> list[str]:
        validate_path(path)
        return await _fs(lambda: sorted(os.listdir(path)))

    async def write_atomic(
        self, path: str, data: bytes, *, mode: int, owner: str, group: str
    ) -> None:
        validate_path(path)
        if not 0 <= mode <= 0o7777:
            raise SystemOpsError("invalid file mode")
        uid, gid = _resolve_uid(owner), _resolve_gid(group)
        await _fs(lambda: write_atomic_sync(path, data, mode=mode, uid=uid, gid=gid))

    async def remove(self, path: str) -> None:
        """Idempotent: removing a missing file is not an error."""
        validate_path(path)

        def _rm() -> None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

        await _fs(_rm)

    async def make_tar_gz(self, dest: str, members: Mapping[str, bytes | LocalFile]) -> None:
        validate_path(dest)
        await _fs(lambda: write_tar_stream_sync(dest, members))

    async def read_tar_gz(self, path: str) -> dict[str, bytes]:
        validate_path(path)
        return await asyncio.to_thread(parse_tar_gz, path)

    async def read_tar_members(
        self, path: str, names: Collection[str] | None = None
    ) -> dict[str, bytes]:
        validate_path(path)
        return await asyncio.to_thread(iter_tar_members, path, names)

    async def extract_tar_member(self, path: str, name: str, dest: str) -> bool:
        validate_path(path)
        return await asyncio.to_thread(extract_tar_member_to_file, path, name, dest)

    async def ensure_dir(self, path: str, mode: int, owner: str, group: str) -> None:
        validate_path(path)
        uid, gid = _resolve_uid(owner), _resolve_gid(group)

        def _mk() -> None:
            os.makedirs(path, mode=mode, exist_ok=True)
            os.chmod(path, mode)
            os.chown(path, uid, gid)

        await _fs(_mk)

    # --- systemd -------------------------------------------------------------------------

    async def systemctl(self, action: str, unit: str) -> None:
        tail = validate_systemctl(action, unit)
        res = await run_command([self._systemctl, *tail], timeout_s=max(self._timeout, 120.0))
        if not res.ok:
            raise SystemOpsError(f"systemctl {action} {unit} failed: {res.output}")

    async def is_active(self, unit: str) -> bool:
        validate_unit(unit)
        res = await run_command([self._systemctl, "is-active", "--quiet", unit], timeout_s=15)
        return res.ok

    async def unit_property(self, unit: str, prop: str) -> str:
        validate_unit(unit)
        validate_property(prop)
        res = await run_command(
            [self._systemctl, "show", "--property", prop, "--value", unit], timeout_s=15
        )
        if not res.ok:
            raise SystemOpsError(f"systemctl show {unit} failed: {res.output}")
        return res.stdout.decode("utf-8", "replace").strip()

    # --- network / health ----------------------------------------------------------------

    async def wait_tcp_open(self, host: str, port: int, timeout_s: float) -> bool:
        if not 0 < port < 65536:
            raise SystemOpsError("invalid port")
        _check_hostname(host)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = max(deadline - loop.time(), 0.05)
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), min(remaining, 1.0)
                )
            except (OSError, TimeoutError):
                pass
            else:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                return True
            if loop.time() + 0.2 >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def http_get(self, url: str, timeout_s: float) -> HttpResult:
        return await asyncio.to_thread(_http_get_sync, url, timeout_s)

    async def resolve(self, hostname: str) -> tuple[list[str], list[str]]:
        _check_hostname(hostname)

        def _resolve() -> tuple[list[str], list[str]]:
            a: list[str] = []
            aaaa: list[str] = []
            try:
                infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            except socket.gaierror:
                return a, aaaa
            for family, _, _, _, sockaddr in infos:
                ip = str(sockaddr[0])
                if family == socket.AF_INET and ip not in a:
                    a.append(ip)
                elif family == socket.AF_INET6 and ip not in aaaa:
                    aaaa.append(ip)
            return a, aaaa

        return await asyncio.to_thread(_resolve)

    async def public_ipv4(self) -> str | None:
        for url in self._ip_probes:
            res = await self.http_get(url, 3.0)
            if res.status == 200:
                try:
                    return str(ipaddress.IPv4Address(res.body.decode("ascii").strip()))
                except (ValueError, UnicodeDecodeError):
                    continue
        return None

    async def tls_cert_info(self, hostname: str) -> CertInfo | None:
        _check_hostname(hostname)
        return await asyncio.to_thread(_tls_cert_info_sync, hostname, self._tls_port, 10.0)

    # --- proxy tooling -------------------------------------------------------------------

    async def tproxy_check(self, config_path: str, profiles_path: str) -> CheckResult:
        validate_path(config_path)
        validate_path(profiles_path)
        res = await run_command(
            [self._tproxy, "-config", config_path, "-profiles-file", profiles_path, "-check"],
            timeout_s=self._timeout,
        )
        return CheckResult(ok=res.ok, output=res.output)

    async def caddy_validate(self, caddyfile_path: str, env: dict[str, str]) -> CheckResult:
        validate_path(caddyfile_path)
        validate_env(env)
        # tgpanel runs with ProtectHome: caddy must not need a writable home (XDG dirs below)
        scratch = CADDY_VALIDATE_DIR
        with contextlib.suppress(OSError):
            await asyncio.to_thread(lambda: os.makedirs(scratch, mode=0o700, exist_ok=True))
        res = await run_command(
            [self._caddy, "validate", "--config", caddyfile_path, "--adapter", "caddyfile"],
            env={
                "HOME": "/root",
                "XDG_DATA_HOME": scratch,
                "XDG_CONFIG_HOME": scratch,
                **env,
            },
            timeout_s=self._timeout,
        )
        return CheckResult(ok=res.ok, output=res.output)

    # --- nftables ------------------------------------------------------------------------

    async def nft_load_file(self, path: str) -> None:
        validate_path(path)
        res = await run_command([self._nft, "-f", path], timeout_s=self._timeout)
        if not res.ok:
            raise SystemOpsError(f"nft -f failed: {res.output}")

    async def nft_check_file(self, path: str) -> CheckResult:
        validate_path(path)
        res = await run_command([self._nft, "-c", "-f", path], timeout_s=self._timeout)
        return CheckResult(ok=res.ok, output=res.output)

    async def nft_list_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        validate_nft_ident(table, "table")
        validate_nft_ident(set_name, "set")
        res = await run_command(
            [self._nft, "-j", "list", "set", "inet", table, set_name], timeout_s=self._timeout
        )
        if not res.ok:
            if "No such file or directory" in res.output:
                raise NftTableMissing(f"nft list set failed: {res.output}")
            raise SystemOpsError(f"nft list set failed: {res.output}")
        return parse_nft_set_json(res.stdout)

    async def nft_delete_table(self, table: str) -> None:
        validate_nft_ident(table, "table")
        res = await run_command(
            [self._nft, "delete", "table", "inet", table], timeout_s=self._timeout
        )
        if not res.ok and "No such file" not in res.output:
            raise SystemOpsError(f"nft delete table failed: {res.output}")

    async def nft_add_elements(self, table: str, set_name: str, ips: list[str]) -> None:
        await self._nft_elements("add", table, set_name, ips)

    async def nft_delete_elements(self, table: str, set_name: str, ips: list[str]) -> None:
        await self._nft_elements("delete", table, set_name, ips)

    async def _nft_elements(self, verb: str, table: str, set_name: str, ips: list[str]) -> None:
        validate_nft_ident(table, "table")
        validate_nft_ident(set_name, "set")
        clean = [validate_ipv4(ip) for ip in ips]
        if not clean:
            return
        elements = "{ " + ", ".join(clean) + " }"
        res = await run_command(
            [self._nft, verb, "element", "inet", table, set_name, elements],
            timeout_s=self._timeout,
        )
        if not res.ok:
            raise SystemOpsError(f"nft {verb} element failed: {res.output}")

    # --- locking -------------------------------------------------------------------------

    async def acquire_lock(self, path: str, timeout_s: float) -> LockHandle:
        """flock(2) on `path`; polled non-blockingly so waiting is cancellable."""
        validate_path(path)
        try:  # opened inline (a local, instant call): no thread hand-off that cancellation could
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # orphan together with the fd
        except OSError as exc:
            raise SystemOpsError(f"cannot open lock file: {exc.strerror}") from None
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SystemOpsError("timed out waiting for lock") from None
                    await asyncio.sleep(0.05)
                except OSError as exc:
                    raise SystemOpsError(f"cannot lock: {exc.strerror}") from None
                else:
                    return RealLockHandle(fd)
        except BaseException:  # timeout, error or cancellation: never leak the descriptor
            os.close(fd)
            raise
