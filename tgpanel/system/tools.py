"""ShellTools: the small second gateway used by installer-side commands (update/doctor/uninstall).

SystemOps stays untouched; everything that SystemOps does not cover (git, pip, journalctl, disk
space, removing our own directory trees) lives here. Same rules as SystemOps: external commands
only as argument lists (never a shell), validated values, scrubbed output. The fake keeps a call
log and supports failure injection so that update/uninstall run in unit tests.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Any, Protocol

from tgpanel.system.ops import SystemOpsError
from tgpanel.system.real import run_command

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,99}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,200}$")
# Trees that uninstall --purge may delete. Backups and /var/lib/caddy are deliberately absent.
REMOVABLE_TREES = frozenset({"/opt/tgpanel", "/etc/tgpanel", "/var/lib/tgpanel"})
_GIT_ENV = {"HOME": "/root", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}


class ShellTools(Protocol):
    async def git_head(self, repo: str) -> str:
        """Full commit id of HEAD."""
        ...

    async def git_fetch(self, repo: str) -> None:
        """`git fetch --tags --prune origin`."""
        ...

    async def git_latest_tag(self, repo: str) -> str | None:
        """Newest tag matching v*.*.* (by version order), or None."""
        ...

    async def nft_chain_exists(self, table: str, chain: str) -> bool:
        """`nft list chain inet <table> <chain>` succeeds."""
        ...

    async def local_ipv6(self) -> list[str]:
        """Global IPv6 addresses configured on this host (without prefix length)."""
        ...

    async def git_resolve(self, repo: str, ref: str) -> str:
        """Commit id of ``origin/<ref>`` (branch) or ``<ref>`` (tag / commit); raises if unknown."""
        ...

    async def git_checkout(self, repo: str, commit: str) -> None:
        """Detached checkout of a commit id."""
        ...

    async def pip_install_locked(self, python: str, lock_file: str) -> None:
        """`pip install --require-hashes -r <lock_file>` with the venv interpreter."""
        ...

    async def migrate_database(self, python: str, repo: str, db_path: str) -> str:
        """Run DB migrations with the (new) code in ``repo``; returns the schema version text."""
        ...

    async def journal_tail(self, unit: str, lines: int) -> str:
        """Last journal lines of a unit (scrubbed)."""
        ...

    async def disk_free(self, path: str) -> int:
        """Free bytes on the filesystem holding ``path``."""
        ...

    async def remove_tree(self, path: str) -> None:
        """Recursively delete one of ``REMOVABLE_TREES`` (anything else is refused)."""
        ...


def _check_ref(ref: str) -> str:
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        raise SystemOpsError("invalid git reference")
    return ref


class RealShellTools:
    def __init__(self, *, timeout_s: float = 120.0, pip_timeout_s: float = 900.0) -> None:
        self._timeout = timeout_s
        self._pip_timeout = pip_timeout_s

    async def _git(self, repo: str, *args: str, timeout_s: float | None = None) -> str:
        res = await run_command(
            ["git", "-C", repo, *args], env=_GIT_ENV, timeout_s=timeout_s or self._timeout
        )
        if not res.ok:
            raise SystemOpsError(f"git {args[0]} failed: {res.output}")
        return res.stdout.decode("utf-8", "replace").strip()

    async def git_head(self, repo: str) -> str:
        return await self._git(repo, "rev-parse", "HEAD")

    async def git_fetch(self, repo: str) -> None:
        await self._git(repo, "fetch", "--tags", "--force", "--prune", "origin", timeout_s=600.0)

    async def git_resolve(self, repo: str, ref: str) -> str:
        _check_ref(ref)
        for candidate in (f"origin/{ref}", ref):
            res = await run_command(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                env=_GIT_ENV,
                timeout_s=self._timeout,
            )
            if res.ok:
                return res.stdout.decode().strip()
        raise SystemOpsError("git reference not found")

    async def git_latest_tag(self, repo: str) -> str | None:
        out = await self._git(repo, "tag", "-l", "v*.*.*", "--sort=-v:refname")
        for line in out.splitlines():
            tag = line.strip()
            if _REF_RE.fullmatch(tag):
                return tag
        return None

    async def nft_chain_exists(self, table: str, chain: str) -> bool:
        if not (_IDENT_RE.fullmatch(table) and _IDENT_RE.fullmatch(chain)):
            raise SystemOpsError("invalid nft identifier")
        res = await run_command(["nft", "list", "chain", "inet", table, chain], timeout_s=30.0)
        return res.ok

    async def local_ipv6(self) -> list[str]:
        res = await run_command(["ip", "-6", "-o", "addr", "show", "scope", "global"])
        if not res.ok:
            return []
        out: list[str] = []
        for line in res.stdout.decode("utf-8", "replace").splitlines():
            fields = line.split()
            if "inet6" in fields:
                addr = fields[fields.index("inet6") + 1].split("/")[0]
                out.append(addr.lower())
        return out

    async def git_checkout(self, repo: str, commit: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SystemOpsError("invalid commit id")
        await self._git(repo, "checkout", "--detach", "--force", commit)

    async def pip_install_locked(self, python: str, lock_file: str) -> None:
        res = await run_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-input",
                "--disable-pip-version-check",
                "-r",
                lock_file,
            ],
            env={"HOME": "/root"},
            timeout_s=self._pip_timeout,
        )
        if not res.ok:
            raise SystemOpsError(f"pip install failed: {res.output}")

    async def migrate_database(self, python: str, repo: str, db_path: str) -> str:
        res = await run_command(
            [python, "-I", "-m", "tgpanel.ops_cli", "migrate", "--db", db_path],
            env={"HOME": "/root", "PYTHONPATH": repo},
            timeout_s=self._timeout,
        )
        if not res.ok:
            raise SystemOpsError(f"migration failed: {res.output}")
        return res.output

    async def journal_tail(self, unit: str, lines: int) -> str:
        if not _UNIT_RE.fullmatch(unit) or unit.startswith("-"):
            raise SystemOpsError("invalid unit name")
        count = max(1, min(int(lines), 200))
        res = await run_command(
            ["journalctl", "-u", unit, "-n", str(count), "--no-pager", "-o", "cat"],
            timeout_s=self._timeout,
        )
        return res.output

    async def disk_free(self, path: str) -> int:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, path)
        except OSError as exc:
            raise SystemOpsError(f"disk usage failed: {exc.strerror}") from None
        return int(usage.free)

    async def remove_tree(self, path: str) -> None:
        if path not in REMOVABLE_TREES:
            raise SystemOpsError("refusing to remove this directory")
        if await asyncio.to_thread(os.path.islink, path):
            raise SystemOpsError("refusing to remove a symbolic link")
        if not await asyncio.to_thread(os.path.exists, path):
            return
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError as exc:
            raise SystemOpsError(f"remove failed: {exc.strerror}") from None


class FakeShellTools:
    """In-memory ShellTools for tests: call log, scripted answers, failure injection."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.head = "a" * 40
        self.refs: dict[str, str] = {}  # ref -> commit id
        self.journal: dict[str, str] = {}
        self.free_bytes = 50 * 1024**3
        self.removed_trees: list[str] = []
        self.fail: dict[str, int] = {}  # method -> remaining failures
        self.migrate_output = "schema version 1"
        # (python, lock_file) -> side effect hook, e.g. to flip service health in tests
        self.on_checkout: list[str] = []
        self.latest_tag: str | None = None
        self.chains: set[tuple[str, str]] = {("tgpanel", "guard"), ("tgpanel", "acct")}
        self.ipv6: list[str] = []

    def fail_on(self, method: str, times: int = 1) -> None:
        self.fail[method] = times

    def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        left = self.fail.get(method, 0)
        if left > 0:
            self.fail[method] = left - 1
            raise SystemOpsError(f"injected failure: {method}")

    def calls_of(self, method: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == method]

    async def git_head(self, repo: str) -> str:
        self._enter("git_head", repo)
        return self.head

    async def git_fetch(self, repo: str) -> None:
        self._enter("git_fetch", repo)

    async def git_resolve(self, repo: str, ref: str) -> str:
        self._enter("git_resolve", repo, ref)
        _check_ref(ref)
        if ref in self.refs:
            return self.refs[ref]
        raise SystemOpsError("git reference not found")

    async def git_latest_tag(self, repo: str) -> str | None:
        self._enter("git_latest_tag", repo)
        return self.latest_tag

    async def nft_chain_exists(self, table: str, chain: str) -> bool:
        self._enter("nft_chain_exists", table, chain)
        return (table, chain) in self.chains

    async def local_ipv6(self) -> list[str]:
        self._enter("local_ipv6")
        return list(self.ipv6)

    async def git_checkout(self, repo: str, commit: str) -> None:
        self._enter("git_checkout", repo, commit)
        self.head = commit
        self.on_checkout.append(commit)

    async def pip_install_locked(self, python: str, lock_file: str) -> None:
        self._enter("pip_install_locked", python, lock_file)

    async def migrate_database(self, python: str, repo: str, db_path: str) -> str:
        self._enter("migrate_database", python, repo, db_path)
        return self.migrate_output

    async def journal_tail(self, unit: str, lines: int) -> str:
        self._enter("journal_tail", unit, lines)
        return self.journal.get(unit, "")

    async def disk_free(self, path: str) -> int:
        self._enter("disk_free", path)
        return self.free_bytes

    async def remove_tree(self, path: str) -> None:
        self._enter("remove_tree", path)
        if path not in REMOVABLE_TREES:
            raise SystemOpsError("refusing to remove this directory")
        self.removed_trees.append(path)
