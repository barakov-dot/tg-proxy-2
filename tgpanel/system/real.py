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
import socket
import ssl
import stat as stat_mod
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tgpanel.system.ops import (
    CertInfo,
    CheckResult,
    FileStat,
    HttpResult,
    LockHandle,
    SetCounter,
    SystemOpsError,
)
from tgpanel.system.validation import (
    build_tar_gz,
    parse_tar_gz,
    scrub,
    validate_env,
    validate_ipv4,
    validate_nft_ident,
    validate_path,
    validate_property,
    validate_systemctl,
    validate_unit,
)

_MINIMAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_MAX_HTTP_BODY = 8 * 1024 * 1024
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


async def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
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
        )
    except OSError as exc:
        raise SystemOpsError(
            f"cannot execute {os.path.basename(argv[0])}: {exc.strerror}"
        ) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        out, err = await proc.communicate()
        return CommandResult(-1, out, err, timed_out=True)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
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
    opener = urllib.request.build_opener(_NoRedirect)
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


def _decode_cert_der(der: bytes) -> dict[str, Any] | None:
    """Decode an unverified DER certificate (CPython-private helper, best effort)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as tmp:
        tmp.write(pem)
        tmp.flush()
        try:
            from ssl import _ssl  # type: ignore[attr-defined]

            decoded: dict[str, Any] = _ssl._test_decode_cert(tmp.name)
        except Exception:
            return None
    return decoded


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


def _tls_cert_info_sync(hostname: str, port: int, timeout_s: float) -> CertInfo | None:
    try:
        ctx = ssl.create_default_context()
        with (
            socket.create_connection((hostname, port), timeout=timeout_s) as sock,
            ctx.wrap_socket(sock, server_hostname=hostname) as tls,
        ):
            cert = tls.getpeercert()
        return _cert_info_from_dict(dict(cert), True) if cert else None
    except ssl.SSLCertVerificationError:
        pass
    except (OSError, ssl.SSLError, ValueError, KeyError):
        return None
    # Chain/name verification failed: still report what the server presents.
    try:
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection((hostname, port), timeout=timeout_s) as sock,
            insecure.wrap_socket(sock, server_hostname=hostname) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
        decoded = _decode_cert_der(der) if der else None
        if decoded:
            return _cert_info_from_dict(decoded, False)
    except (OSError, ssl.SSLError, ValueError, KeyError):
        return None
    return CertInfo(issuer="", not_before="", not_after="", valid_chain=False)


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

    async def make_tar_gz(self, dest: str, members: dict[str, bytes]) -> None:
        validate_path(dest)
        blob = await asyncio.to_thread(build_tar_gz, members)
        await _fs(
            lambda: write_atomic_sync(dest, blob, mode=0o600, uid=os.geteuid(), gid=os.getegid())
        )

    async def read_tar_gz(self, path: str) -> dict[str, bytes]:
        validate_path(path)
        return await asyncio.to_thread(parse_tar_gz, path)

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
        res = await run_command(
            [self._caddy, "validate", "--config", caddyfile_path, "--adapter", "caddyfile"],
            env={"HOME": "/root", **env},
            timeout_s=self._timeout,
        )
        return CheckResult(ok=res.ok, output=res.output)

    # --- nftables ------------------------------------------------------------------------

    async def nft_load_file(self, path: str) -> None:
        validate_path(path)
        res = await run_command([self._nft, "-f", path], timeout_s=self._timeout)
        if not res.ok:
            raise SystemOpsError(f"nft -f failed: {res.output}")

    async def nft_list_set(self, table: str, set_name: str) -> dict[str, SetCounter]:
        validate_nft_ident(table, "table")
        validate_nft_ident(set_name, "set")
        res = await run_command(
            [self._nft, "-j", "list", "set", "inet", table, set_name], timeout_s=self._timeout
        )
        if not res.ok:
            raise SystemOpsError(f"nft list set failed: {res.output}")
        return parse_nft_set_json(res.stdout)

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
        try:
            fd = await asyncio.to_thread(os.open, path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise SystemOpsError(f"cannot open lock file: {exc.strerror}") from None
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise SystemOpsError("timed out waiting for lock") from None
                try:
                    await asyncio.sleep(0.05)
                except BaseException:
                    os.close(fd)
                    raise
            except OSError as exc:
                os.close(fd)
                raise SystemOpsError(f"cannot lock: {exc.strerror}") from None
            else:
                return RealLockHandle(fd)
