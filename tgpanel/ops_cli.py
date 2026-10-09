"""Operations CLI: doctor, repair, update, uninstall, show-url, reset-password and the internal
helpers used by install.sh (caddy-install, bootstrap, pre-install-backup, migrate).

Wiring (done by the orchestrator in tgpanel/cli.py)::

    from tgpanel import ops_cli
    ops_cli.register(sub, lambda: build_context(ops, db_path, config=cfg))
    ...
    if getattr(args, "ops_run", None):
        return args.ops_run(args, stream, input_fn)

``ctx_factory`` must return a FRESH AppContext; the runner closes it. The module also runs
standalone (``python -m tgpanel.ops_cli <command>``), which is what install.sh uses.

Every state change goes through SystemOps / ShellTools; proxy-side files are touched only in
the ways PLAN 12 allows: our Caddyfile block, and (uninstall) restoring profiles.json /
config.json from the pre-install archive. User-facing text is Russian.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from tgpanel.apply import backup as backup_mod
from tgpanel.apply.backup import PRE_INSTALL, BackupError, scan_disk_backups
from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.db.connection import SchemaTooNewError, current_version, transaction
from tgpanel.doctor import (
    PANEL_PORT,
    DoctorEnv,
    describe_cert,
    read_caddy_env,
    read_install_env,
    run_doctor,
    stored_cert_exists,
    wait_for_certificate,
)
from tgpanel.render.caddy import insert_panel_block, remove_panel_block
from tgpanel.render.errors import RenderError
from tgpanel.render.nft import render_firewall_unit
from tgpanel.services.container import AppContext
from tgpanel.system.ops import SystemOps, SystemOpsError
from tgpanel.system.tools import REMOVABLE_TREES, RealShellTools, ShellTools
from tgpanel.system.validation import scrub

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PENDING = 3  # work done, but something (certificate) is still pending

ACTOR = "system"
INSTALL_DIR = "/opt/tgpanel"
VENV_PYTHON = f"{INSTALL_DIR}/.venv/bin/python"
CLI_WRAPPER = "/usr/local/bin/tgpanel"
DEFAULT_DB = "/var/lib/tgpanel/tgpanel.db"
DEFAULT_REF = "main"
PANEL_UNIT = "tgpanel"
POOL_TEMPLATE = "tgpanel-mtproxy@.service"
BOOTSTRAP_PASSWORD_ENV = "TGPANEL_BOOTSTRAP_PASSWORD"  # noqa: S105 - variable name
SETTING_LOGIN = "panel_login"
SETTING_PASSWORD_HASH = "panel_password_hash"  # noqa: S105 - setting name
SETTING_SESSION_VERSION = "panel_session_version"
SETTING_BOOTSTRAPPED = "install.bootstrapped"

# Units shipped in deploy/ that we own: tgpanel*.service|path|timer|socket (never the pool
# template tgpanel-mtproxy@.service, which the apply pipeline renders). The firewall unit is
# rendered from code, the CLI wrapper is deploy/tgpanel-cli.
_OWNED_UNIT_RE = re.compile(r"^tgpanel[A-Za-z0-9._-]*\.(service|path|timer|socket)$")
FIREWALL_UNIT_NAME = "tgpanel-firewall.service"
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,99}$")


@dataclass
class Runtime:
    """Everything a command needs; tests inject fakes and zero-delay sleeps."""

    ctx: AppContext
    tools: ShellTools
    out: TextIO
    input_fn: Callable[[str], str]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    install_dir: str = INSTALL_DIR
    db_path: str = DEFAULT_DB

    @property
    def ops(self) -> SystemOps:
        return self.ctx.pipeline.ops

    @property
    def config(self) -> ApplyConfig:
        return self.ctx.pipeline.config

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    @property
    def venv_python(self) -> str:
        return f"{self.install_dir}/.venv/bin/python"


def _clean(text: str) -> str:
    return scrub(" ".join(text.split()), 400)


def _confirm(rt: Runtime, prompt: str) -> bool:
    return rt.input_fn(prompt).strip().lower() in ("y", "yes", "д", "да")


# ----------------------------------------------------------------------- Caddy block


@dataclass(frozen=True, slots=True)
class CaddyOutcome:
    ok: bool
    changed: bool = False
    error: str = ""


async def _restart_caddy_checked(rt: Runtime) -> None:
    await rt.ops.systemctl("restart", "caddy")
    if not await rt.ops.is_active("caddy"):
        raise SystemOpsError("caddy не запустился после перезапуска")


async def _rewrite_caddyfile(
    rt: Runtime, transform: Callable[[str], str], *, restart_if_unchanged: bool = False
) -> CaddyOutcome:
    """Same as below, under the global apply lock (no concurrent apply / restore)."""
    try:
        lock = await rt.ops.acquire_lock(rt.config.paths.lock, rt.config.timing.lock_timeout_s)
    except SystemOpsError as exc:
        return CaddyOutcome(False, error=f"блокировка применения недоступна: {_clean(str(exc))}")
    try:
        return await _rewrite_caddyfile_locked(
            rt, transform, restart_if_unchanged=restart_if_unchanged
        )
    finally:
        with contextlib.suppress(Exception):
            await lock.release()


async def _rewrite_caddyfile_locked(
    rt: Runtime, transform: Callable[[str], str], *, restart_if_unchanged: bool = False
) -> CaddyOutcome:
    """Backup-in-memory -> render -> `caddy validate` (drop-in env) -> atomic write -> restart.

    Any failure leaves (or puts back) the original Caddyfile; if caddy was already restarted
    with the new file it is restarted again with the old one.
    """
    ops, cfg = rt.ops, rt.config
    path = cfg.paths.caddyfile
    try:
        st = await ops.stat(path)
        original = await ops.read_file(path)
    except SystemOpsError as exc:
        return CaddyOutcome(False, error=f"Caddyfile не читается: {_clean(str(exc))}")
    text = original.decode("utf-8", "replace")
    try:
        new_text = transform(text)
    except RenderError as exc:
        return CaddyOutcome(False, error=_clean(str(exc)))
    if new_text == text and not restart_if_unchanged:
        return CaddyOutcome(True, changed=False)
    caddy_env = await read_caddy_env(ops, cfg)
    check_path = f"{path.rsplit('/', 1)[0]}/.tgpanel-check-Caddyfile"
    try:
        await ops.write_atomic(
            check_path, new_text.encode(), mode=0o600, owner=st.owner, group=st.group
        )
        verdict = await ops.caddy_validate(check_path, caddy_env)
    except SystemOpsError as exc:
        return CaddyOutcome(False, error=_clean(str(exc)))
    finally:
        with contextlib.suppress(SystemOpsError):
            await ops.remove(check_path)
    if not verdict.ok:
        return CaddyOutcome(False, error=f"caddy validate отклонил конфигурацию: {verdict.output}")
    if new_text == text:
        try:
            await _restart_caddy_checked(rt)
        except SystemOpsError as exc:
            return CaddyOutcome(False, error=_clean(str(exc)))
        return CaddyOutcome(True, changed=False)
    try:
        await ops.write_atomic(
            path, new_text.encode(), mode=st.mode, owner=st.owner, group=st.group
        )
        await _restart_caddy_checked(rt)
    except SystemOpsError as exc:
        error = _clean(str(exc))
        try:  # roll back: the proxy block must keep working
            await ops.write_atomic(path, original, mode=st.mode, owner=st.owner, group=st.group)
            await _restart_caddy_checked(rt)
        except SystemOpsError as exc2:
            error += f"; откат Caddyfile не удался: {_clean(str(exc2))}"
        return CaddyOutcome(False, error=error + " (Caddyfile возвращён)")
    return CaddyOutcome(True, changed=True)


async def install_caddy_block(rt: Runtime, domain: str, path: str) -> CaddyOutcome:
    """Insert or refresh the tgpanel block (idempotent: an identical block changes nothing)."""
    return await _rewrite_caddyfile(rt, lambda text: insert_panel_block(text, domain, path))


async def remove_caddy_block(rt: Runtime) -> CaddyOutcome:
    return await _rewrite_caddyfile(rt, remove_panel_block)


# ------------------------------------------------------------------------ static units


async def desired_static_files(rt: Runtime) -> dict[str, tuple[bytes, int]]:
    """Unit files and the CLI wrapper of THIS tree: path -> (content, mode)."""
    ops = rt.ops
    sysd = rt.config.paths.systemd_dir
    deploy = f"{rt.install_dir}/deploy"
    files: dict[str, tuple[bytes, int]] = {}
    for name in sorted(await ops.list_dir(deploy)):
        if _OWNED_UNIT_RE.match(name) and "@" not in name and name != FIREWALL_UNIT_NAME:
            files[f"{sysd}/{name}"] = (await ops.read_file(f"{deploy}/{name}"), 0o644)
    if f"{sysd}/tgpanel.service" not in files:
        raise SystemOpsError("в deploy нет tgpanel.service")
    files[f"{sysd}/{FIREWALL_UNIT_NAME}"] = (render_firewall_unit(), 0o644)
    files[CLI_WRAPPER] = (await ops.read_file(f"{deploy}/tgpanel-cli"), 0o755)
    return files


async def _obsolete_units(rt: Runtime, wanted: dict[str, tuple[bytes, int]]) -> list[str]:
    """Our unit files in /etc/systemd/system that the new tree no longer ships."""
    sysd = rt.config.paths.systemd_dir
    try:
        names = await rt.ops.list_dir(sysd)
    except SystemOpsError:
        return []
    return [
        f"{sysd}/{n}"
        for n in sorted(names)
        if _OWNED_UNIT_RE.match(n) and "@" not in n and f"{sysd}/{n}" not in wanted
    ]


async def sync_static_files(rt: Runtime) -> list[str]:
    """Write changed unit files and remove obsolete ones of ours; restore all on failure.

    Returns the changed paths. daemon-reload runs when a unit file changed.
    """
    ops = rt.ops
    wanted = await desired_static_files(rt)
    sysd = rt.config.paths.systemd_dir
    changed: list[tuple[str, bytes | None]] = []
    removed: list[tuple[str, bytes]] = []
    try:
        for target, (data, mode) in wanted.items():
            try:
                current: bytes | None = await ops.read_file(target)
            except SystemOpsError:
                current = None
            if current == data:
                continue
            await ops.write_atomic(target, data, mode=mode, owner="root", group="root")
            changed.append((target, current))
        for target in await _obsolete_units(rt, wanted):
            old = await ops.read_file(target)
            with contextlib.suppress(SystemOpsError):
                await ops.systemctl("disable-now", target.rsplit("/", 1)[-1])
            await ops.remove(target)
            removed.append((target, old))
        if any(t.startswith(sysd + "/") for t, _ in changed) or removed:
            await ops.systemctl("daemon-reload", "")
    except SystemOpsError:
        for target, previous in reversed(changed):
            with contextlib.suppress(SystemOpsError):
                if previous is None:
                    await ops.remove(target)
                else:
                    mode = wanted[target][1]
                    await ops.write_atomic(target, previous, mode=mode, owner="root", group="root")
        for target, previous in removed:
            with contextlib.suppress(SystemOpsError):
                await ops.write_atomic(target, previous, mode=0o644, owner="root", group="root")
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("daemon-reload", "")
        raise
    return [t for t, _ in changed] + [t for t, _ in removed]


async def enable_static_units(rt: Runtime) -> None:
    """Enable every shipped unit that has an [Install] section (paths/timers also start)."""
    ops = rt.ops
    for target, (data, _mode) in (await desired_static_files(rt)).items():
        name = target.rsplit("/", 1)[-1]
        if not _OWNED_UNIT_RE.match(name) or b"[Install]" not in data:
            continue
        if name.endswith((".path", ".timer")):
            await ops.systemctl("enable-now", name)
        else:
            await ops.systemctl("enable", name)


# -------------------------------------------------------------------------- commands


async def cmd_doctor(rt: Runtime, args: argparse.Namespace) -> int:
    with contextlib.suppress(SystemOpsError):
        await rt.ctx.start(recover=False)
    env = DoctorEnv(rt.ops, rt.tools, rt.config, rt.ctx, clock=rt.clock)
    report = await run_doctor(env)
    rt.say("Диагностика tgpanel")
    rt.say()
    report.render(rt.out)
    return report.exit_code


async def cmd_repair(rt: Runtime, args: argparse.Namespace) -> int:
    ops = rt.ops
    rt.say("Восстановление компонентов tgpanel…")
    try:
        info = await rt.ctx.pipeline.create_backup("pre-repair", ACTOR)
    except BackupError as exc:
        rt.say(f"Резервная копия не создана, восстановление остановлено: {exc}")
        return EXIT_ERROR
    rt.say(f"Резервная копия: {info.path}")
    failures = 0

    try:
        changed = await sync_static_files(rt)
        await enable_static_units(rt)
        rt.say(
            "Юниты: восстановлено " + ", ".join(p.rsplit("/", 1)[-1] for p in changed)
            if changed
            else "Юниты: в порядке"
        )
    except SystemOpsError as exc:
        failures += 1
        rt.say(f"Юниты: не удалось ({_clean(str(exc))}); прежние файлы возвращены")

    env = await read_install_env(ops, rt.config)
    domain, path = env.get("TGPANEL_PANEL_DOMAIN", ""), env.get("TGPANEL_PANEL_PATH", "")
    if domain and path:
        outcome = await install_caddy_block(rt, domain, path)
        if not outcome.ok:
            failures += 1
            rt.say(f"Блок Caddy: не удалось ({outcome.error})")
        else:
            rt.say(
                "Блок Caddy: возвращён, Caddy перезапущен"
                if outcome.changed
                else "Блок Caddy: на месте"
            )
    else:
        failures += 1
        rt.say("Блок Caddy: домен или путь панели неизвестны (нет tgpanel.env)")

    apply_outcome = await rt.ctx.pipeline.apply_now("repair", ACTOR, full_nft_reload=True)
    if apply_outcome.status in ("needs_adoption", "external_change"):
        failures += 1
        rt.say(f"Таблица nft и юнит пулов: применение отложено — {apply_outcome.error}")
    elif not apply_outcome.ok:
        failures += 1
        rt.say(f"Таблица nft и юнит пулов: не удалось ({apply_outcome.error}); состояние отката")
    else:
        rt.say("Таблица nft, юнит пулов: загружены заново")
    rt.say()
    rt.say("Готово." if not failures else f"Не всё удалось восстановить ({failures}); см. выше.")
    return EXIT_OK if not failures else EXIT_ERROR


AUTO_REF = "auto"


async def _read_state_ref(rt: Runtime) -> str:
    try:
        text = (await rt.ops.read_file(f"{rt.config.paths.state_dir}/ref")).decode().strip()
    except SystemOpsError:
        return AUTO_REF
    return text if text == AUTO_REF or _REF_RE.fullmatch(text) else AUTO_REF


async def _resolve_requested_ref(rt: Runtime, requested: str) -> str:
    """``auto`` = newest v*.*.* tag if any, else the ``main`` branch (printed to the user)."""
    if requested != AUTO_REF:
        return requested
    tag = await rt.tools.git_latest_tag(rt.install_dir)
    if tag:
        rt.say(f"Версия: последний релиз {tag}")
        return tag
    rt.say(
        f"Релизных тегов нет: берётся движущаяся ветка {DEFAULT_REF}. "
        "Для фиксации версии укажите --ref <тег или коммит>."
    )
    return DEFAULT_REF


async def _switch_code(rt: Runtime, commit: str) -> None:
    lock = f"{rt.install_dir}/requirements.lock"
    await rt.tools.git_checkout(rt.install_dir, commit)
    await rt.tools.pip_install_locked(rt.venv_python, lock)


async def _panel_healthy(rt: Runtime) -> bool:
    ops = rt.ops
    try:
        return await ops.is_active(PANEL_UNIT) and await ops.wait_tcp_open(
            "127.0.0.1", PANEL_PORT, 30.0
        )
    except SystemOpsError:
        return False


_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+")


def _db_schema_version(path: str) -> int:
    """Schema version of a database file, read with a plain connection (0 if unreadable)."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


async def _restore_db_snapshot(rt: Runtime, snapshot: str) -> bool:
    """Put the pre-update database back (sqlite backup API into the live connection)."""
    import sqlite3

    tmp_dir = backup_mod.make_temp_dir()

    def restore(conn: sqlite3.Connection, local: str) -> None:
        src = sqlite3.connect(local)
        try:
            src.backup(conn)
        finally:
            src.close()

    try:
        local = tmp_dir / "restore.db"
        await asyncio.to_thread(local.write_bytes, await rt.ops.read_file(snapshot))
        await rt.ctx.db.run(restore, str(local))
    except (sqlite3.Error, SystemOpsError, OSError):
        return False
    finally:
        backup_mod.remove_temp_dir(tmp_dir)
    return True


async def _verify_requested_tag(rt: Runtime, ref: str) -> str | None:
    """Opt-in signature check of a tag against deploy/trusted-signers; error text or None."""
    if not _TAG_RE.match(ref):
        return "--verify-tag работает только с тегами вида vX.Y.Z"
    keys = f"{rt.install_dir}/deploy/trusted-signers"
    try:
        text = (await rt.ops.read_file(keys)).decode("utf-8", "replace")
    except SystemOpsError:
        text = ""
    if "BEGIN PGP PUBLIC KEY BLOCK" not in text:
        return "файл deploy/trusted-signers пуст: нет ключей для проверки подписи тега"
    ok, detail = await rt.tools.git_verify_tag(rt.install_dir, ref, keys)
    return None if ok else f"подпись тега {ref} не подтверждена: {_clean(detail)}"


async def cmd_update(rt: Runtime, args: argparse.Namespace) -> int:
    """Stage 1 (OLD code): resolve, back up, stop, check out + install the new code, then hand
    over to a NEW interpreter (``post-update``). Any failure rolls everything back."""
    ops, tools = rt.ops, rt.tools
    requested = args.ref or await _read_state_ref(rt)
    if requested != AUTO_REF and (not _REF_RE.fullmatch(requested) or ".." in requested):
        rt.say("Некорректное имя ветки, тега или коммита.")
        return EXIT_USAGE
    try:
        previous = await tools.git_head(rt.install_dir)
        await tools.git_fetch(rt.install_dir)
        ref = await _resolve_requested_ref(rt, requested)
        target = await tools.git_resolve(rt.install_dir, ref)
    except SystemOpsError as exc:
        rt.say(f"Не удалось получить обновление: {_clean(str(exc))}")
        return EXIT_ERROR
    if target == previous and not args.force:
        rt.say(f"Уже установлена версия {previous} ({ref}); обновлять нечего.")
        return EXIT_OK
    try:
        downgrade = target != previous and await tools.git_is_ancestor(
            rt.install_dir, target, previous
        )
    except SystemOpsError:
        downgrade = False
    if downgrade and not args.allow_downgrade:
        rt.say(
            f"Версия {ref} ({target[:10]}) старше установленной ({previous[:10]}): откат версии "
            "может сломать схему базы. Если это нужно, добавьте --allow-downgrade."
        )
        return EXIT_ERROR
    if args.verify_tag or os.environ.get("TGPANEL_VERIFY_TAG") == "1":
        problem = await _verify_requested_tag(rt, ref)
        if problem:
            rt.say(f"Обновление остановлено: {problem}")
            return EXIT_ERROR
        rt.say(f"Подпись тега {ref} подтверждена.")
    rt.say(f"Обновление {previous[:10]} → {target} ({ref})")

    # --- backups: archive + a plain database snapshot (restored if the schema moves forward)
    try:
        info = await rt.ctx.pipeline.create_backup("pre-update", ACTOR)
        rt.say(f"Резервная копия: {info.path}")
    except BackupError as exc:
        rt.say(f"Резервная копия не создана, обновление остановлено: {exc}")
        return EXIT_ERROR
    stamp = rt.clock().strftime("%Y%m%dT%H%M%SZ")
    snapshot = f"{rt.config.paths.backups_dir}/{stamp}-pre-update.db"
    tmp_dir = backup_mod.make_temp_dir()
    try:
        local = tmp_dir / "snapshot.db"
        await asyncio.to_thread(rt.ctx.db.snapshot_to, local)
        await ops.write_atomic(
            snapshot,
            await asyncio.to_thread(local.read_bytes),
            mode=0o600,
            owner="root",
            group="root",
        )
    except Exception as exc:
        rt.say(f"Снимок базы данных не создан, обновление остановлено: {_clean(str(exc))}")
        return EXIT_ERROR
    finally:
        backup_mod.remove_temp_dir(tmp_dir)
    schema_before = await rt.ctx.db.run(current_version)

    # --- no mutating operations while the code is swapped
    with contextlib.suppress(SystemOpsError):
        await ops.systemctl("stop", PANEL_UNIT)

    async def roll_back(reason: str) -> int:
        rt.say(f"Ошибка: {reason}. Возвращаю версию {previous[:10]}…")
        try:
            await _switch_code(rt, previous)
            if _db_schema_version(rt.db_path) > schema_before:
                restored = await _restore_db_snapshot(rt, snapshot)
                rt.say(
                    "База данных возвращена к состоянию до обновления."
                    if restored
                    else f"НЕ УДАЛОСЬ вернуть базу данных; снимок: {snapshot}"
                )
            await sync_static_files(rt)
            await enable_static_units(rt)
            await ops.systemctl("restart", PANEL_UNIT)
            if await _panel_healthy(rt):
                rt.say("Предыдущая версия запущена.")
            else:
                rt.say("Предыдущая версия возвращена, но панель не отвечает: tgpanel doctor.")
        except SystemOpsError as exc:
            rt.say(f"Откат не удался: {_clean(str(exc))}; снимок базы: {snapshot}")
        return EXIT_ERROR

    try:
        await _switch_code(rt, target)
        rc, output = await tools.run_post_update(
            rt.venv_python, rt.install_dir, rt.db_path, previous, target
        )
    except SystemOpsError as exc:
        return await roll_back(_clean(str(exc)))
    if output.strip():
        rt.say(output.strip())
    if rc != 0:
        return await roll_back(f"этап 2 обновления завершился с кодом {rc}")
    if not await _panel_healthy(rt):
        return await roll_back("панель не запустилась после обновления")
    with contextlib.suppress(SystemOpsError):
        await ops.write_atomic(
            f"{rt.config.paths.state_dir}/ref",
            f"{requested}\n".encode(),
            mode=0o600,
            owner="root",
            group="root",
        )
        await ops.remove(snapshot)
    rt.say(f"Обновление выполнено, панель работает. Установлена версия {target}.")
    return EXIT_OK


async def cmd_post_update(rt: Runtime, args: argparse.Namespace) -> int:
    """Stage 2 (NEW code, new process): migrations ran when the database was opened; now units,
    the apply and the panel restart. Non-zero makes stage 1 roll back."""
    ops = rt.ops
    version = await rt.ctx.db.run(current_version)
    rt.say(f"Этап 2: новый код {args.new[:10]}, схема БД {version}")
    try:
        changed = await sync_static_files(rt)
        await enable_static_units(rt)
    except SystemOpsError as exc:
        rt.say(f"Юниты не обновлены: {_clean(str(exc))}")
        return EXIT_ERROR
    if changed:
        rt.say("Обновлены файлы: " + ", ".join(p.rsplit("/", 1)[-1] for p in changed))
    outcome = await rt.ctx.pipeline.apply_now("update", ACTOR)
    if outcome.status in ("needs_adoption", "external_change"):
        rt.say(f"Применение отложено (не ошибка обновления): {outcome.error}")
    elif not outcome.ok:
        rt.say(f"Применение после обновления не удалось: {outcome.error}")
        return EXIT_ERROR
    try:
        await ops.systemctl("restart", PANEL_UNIT)
    except SystemOpsError as exc:
        rt.say(f"Панель не запустилась: {_clean(str(exc))}")
        return EXIT_ERROR
    if not await _panel_healthy(rt):
        rt.say("Панель не открыла порт 8090 после перезапуска.")
        return EXIT_ERROR
    return EXIT_OK


async def cmd_install_units(rt: Runtime, args: argparse.Namespace) -> int:
    """Internal (install.sh stage 2): unit files + CLI wrapper + enable."""
    try:
        changed = await sync_static_files(rt)
        await enable_static_units(rt)
    except SystemOpsError as exc:
        rt.say(f"Юниты не установлены: {_clean(str(exc))}")
        return EXIT_ERROR
    rt.say(
        "Юниты установлены: "
        + (", ".join(p.rsplit("/", 1)[-1] for p in changed) or "без изменений")
    )
    return EXIT_OK


async def _lock_or_say(rt: Runtime) -> Any:
    try:
        return await rt.ops.acquire_lock(rt.config.paths.lock, rt.config.timing.lock_timeout_s)
    except SystemOpsError as exc:
        rt.say(f"Не удалось получить блокировку применения: {_clean(str(exc))}")
        return None


async def _release(lock: Any) -> None:
    with contextlib.suppress(Exception):
        await lock.release()


async def cmd_uninstall(rt: Runtime, args: argparse.Namespace) -> int:
    ops = rt.ops
    paths = rt.config.paths
    rt.say("Удаление tgpanel возвращает прокси к состоянию до установки панели.")
    rt.say("/var/lib/caddy не трогается.")
    archives = [b for b in await scan_disk_backups(ops, paths) if b.reason == PRE_INSTALL]
    archive = min(archives, key=lambda b: b.created_at) if archives else None
    if archive is None and not args.force:
        rt.say("Копия pre-install не найдена: восстановить исходные профили прокси не из чего.")
        rt.say(
            "Удаление остановлено, ничего не изменено. С ключом --force панель будет удалена, "
            "а профили relay пересобраны из базы данных и направлены на старый процесс MTProxy "
            "(порт 2398): продолжат работать только импортированные пользователи, "
            "созданные в панели — нет."
        )
        return EXIT_ERROR
    if archive is not None:
        rt.say(
            "Пользователи, созданные панелью, перестанут работать (профили вернутся к pre-install)."
        )
    if args.purge:
        rt.say("ВНИМАНИЕ: --purge удалит базу данных, настройки и каталог /opt/tgpanel.")
    if not args.yes and not _confirm(rt, "Продолжить? [y/N] "):
        rt.say("Отменено.")
        return EXIT_ERROR

    # 0. stop the panel first: nothing may re-apply while we take things apart
    with contextlib.suppress(SystemOpsError):
        await ops.systemctl("disable-now", PANEL_UNIT)

    async def abort(text: str) -> int:
        rt.say(text)
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("enable-now", PANEL_UNIT)
        return EXIT_ERROR

    try:
        info = await rt.ctx.pipeline.create_backup("pre-uninstall", ACTOR)
        rt.say(f"Резервная копия перед удалением: {info.path}")
    except BackupError as exc:
        return await abort(f"Не удалось создать резервную копию: {exc}")

    # 1. the legacy MTProxy must serve the relay again before the relay points at it
    try:
        rt.say(await rt.ctx.pipeline.set_legacy_mtproxy(True, ACTOR))
        if not await ops.wait_tcp_open(
            "127.0.0.1", rt.config.legacy_port, rt.config.timing.port_timeout_s
        ):
            return await abort(f"Старый MTProxy не открыл порт {rt.config.legacy_port}.")
    except (OperationRejected, SystemOpsError) as exc:
        return await abort(f"Не удалось включить старый MTProxy: {_clean(str(exc))}")

    # 2. proxy files + our components, under the global apply lock
    lock = await _lock_or_say(rt)
    if lock is None:
        return await abort("Удаление остановлено.")
    try:
        if archive is not None:
            ok = await _restore_proxy_files(rt, archive.path, assume_yes=args.yes)
        else:
            ok = await _rebuild_profiles_for_legacy(rt, assume_yes=args.yes)
        if not ok:
            return await abort("Профили прокси не изменены; панель оставлена.")
        problems = await _remove_our_components(rt)
    finally:
        await _release(lock)

    # 3. the Caddy block (proxy block and /var/lib/caddy stay untouched)
    outcome = await remove_caddy_block(rt)
    if not outcome.ok:
        problems.append(f"блок Caddy не удалён: {outcome.error}")
    elif outcome.changed:
        rt.say("Блок панели удалён из Caddyfile, Caddy перезапущен.")

    # 4. a reinstall must take a fresh snapshot: the used archive is renamed
    if archive is not None:
        used = await _mark_archive_used(rt, archive.path)
        if used:
            rt.say(f"Архив pre-install помечен использованным: {used}")
        else:
            problems.append("архив pre-install не удалось переименовать")

    # 5. data
    if args.purge:
        for tree in sorted(REMOVABLE_TREES):
            try:
                await rt.tools.remove_tree(tree)
            except SystemOpsError as exc:
                problems.append(f"{tree}: {_clean(str(exc))}")
        with contextlib.suppress(SystemOpsError):
            await ops.remove(CLI_WRAPPER)
        rt.say(
            f"Данные удалены. Резервные копии остались в {paths.backups_dir}: в них есть секреты "
            "пользователей и токен бота (архивы 0600). Удалите их вручную, если они не нужны."
        )
    else:
        rt.say(
            f"Данные сохранены: база {DEFAULT_DB}, настройки {paths.tgpanel_dir}, резервные копии."
        )
    for problem in problems:
        rt.say(f"Предупреждение: {problem}")
    rt.say("Готово." if not problems else "Удаление завершено с предупреждениями.")
    return EXIT_OK if not problems else EXIT_ERROR


async def _mark_archive_used(rt: Runtime, path: str) -> str | None:
    ops = rt.ops
    target = path.replace("-pre-install.tar.gz", "-pre-install-used.tar.gz")
    n = 1
    while target != path and await ops.exists(target):
        n += 1
        target = path.replace("-pre-install.tar.gz", f"-pre-install-used.{n}.tar.gz")
    if target == path:
        return None
    try:
        data = await ops.read_file(path)
        await ops.write_atomic(target, data, mode=0o600, owner="root", group="root")
        await ops.remove(path)
    except SystemOpsError:
        return None
    return target


_RESTORE_MEMBERS = ("etc/tproxy-server/profiles.json", "etc/tproxy-server/config.json")


async def _wait_healthz(rt: Runtime) -> bool:
    t = rt.config.timing
    for attempt in range(t.healthz_attempts):
        try:
            res = await rt.ops.http_get(f"{rt.config.admin_url}/healthz", t.http_timeout_s)
        except SystemOpsError:
            res = None
        if res is not None and res.status == 200:
            return True
        if attempt < t.healthz_attempts - 1:
            await rt.sleep(t.healthz_interval_s)
    return False


async def _install_proxy_files(
    rt: Runtime,
    new_profiles: bytes,
    new_config: bytes | None,
    attrs: dict[str, tuple[int, str, str]],
) -> bool:
    """Check (`tproxy-server -check` on temp copies), write, restart the relay, roll back.

    Caller holds the global apply lock. ``attrs`` = path -> (mode, owner, group).
    """
    ops, cfg = rt.ops, rt.config
    paths = cfg.paths
    pm, po, pg = attrs[paths.profiles]
    cm, co, cg = attrs[paths.config]
    try:
        before = {p: await ops.read_file(p) for p in (paths.profiles, paths.config)}
        config_bytes = new_config if new_config is not None else before[paths.config]
        await ops.write_atomic(paths.check_profiles, new_profiles, mode=0o600, owner=po, group=pg)
        await ops.write_atomic(paths.check_config, config_bytes, mode=0o600, owner=co, group=cg)
        try:
            verdict = await ops.tproxy_check(paths.check_config, paths.check_profiles)
        finally:
            for tmp in (paths.check_profiles, paths.check_config):
                with contextlib.suppress(SystemOpsError):
                    await ops.remove(tmp)
    except SystemOpsError as exc:
        rt.say(f"Проверка файлов не выполнена: {_clean(str(exc))}")
        return False
    if not verdict.ok:
        rt.say(f"relay отклонил новые файлы профилей: {verdict.output}")
        return False
    try:
        if new_config is not None:
            await ops.write_atomic(paths.config, new_config, mode=cm, owner=co, group=cg)
        await ops.write_atomic(paths.profiles, new_profiles, mode=pm, owner=po, group=pg)
        await ops.systemctl("restart", cfg.relay_unit)
        healthy = await _wait_healthz(rt)
    except SystemOpsError as exc:
        healthy = False
        rt.say(f"Ошибка при записи: {_clean(str(exc))}")
    if not healthy:
        rt.say("relay не ответил после замены профилей; возвращаю прежние файлы.")
        with contextlib.suppress(SystemOpsError):
            await ops.write_atomic(paths.config, before[paths.config], mode=cm, owner=co, group=cg)
            await ops.write_atomic(
                paths.profiles, before[paths.profiles], mode=pm, owner=po, group=pg
            )
            await ops.systemctl("restart", cfg.relay_unit)
        return False
    return True


async def _file_attrs(rt: Runtime) -> dict[str, tuple[int, str, str]]:
    paths = rt.config.paths
    out: dict[str, tuple[int, str, str]] = {}
    for path, default_mode in ((paths.profiles, 0o400), (paths.config, 0o640)):
        try:
            st = await rt.ops.stat(path)
            out[path] = (st.mode, st.owner, st.group)
        except SystemOpsError:
            out[path] = (default_mode, "root", rt.config.tproxy_group)
    return out


def _merge_limits(current: bytes, archived: bytes) -> bytes:
    """current config.json with ONLY the ``limits`` block taken from the archive."""
    import json

    cur = json.loads(current)
    old = json.loads(archived)
    if not isinstance(cur, dict) or not isinstance(old, dict):
        raise ValueError("config.json must be an object")
    if "limits" in old:
        cur["limits"] = old["limits"]
    else:
        cur.pop("limits", None)
    return (json.dumps(cur, indent=2, ensure_ascii=False) + "\n").encode()


async def _restore_proxy_files(rt: Runtime, archive_path: str, *, assume_yes: bool) -> bool:
    """profiles.json from the pre-install archive and ONLY ``limits`` of config.json."""
    from tgpanel.domain.secrets_ import base_secret
    from tgpanel.render.profiles import parse_profiles

    ops, paths = rt.ops, rt.config.paths
    try:
        members = await ops.read_tar_members(
            archive_path, {backup_mod.MANIFEST_NAME, *_RESTORE_MEMBERS}
        )
    except SystemOpsError as exc:
        rt.say(f"Архив pre-install не читается: {_clean(str(exc))}")
        return False
    old_profiles = members.get(_RESTORE_MEMBERS[0])
    old_config = members.get(_RESTORE_MEMBERS[1])
    if old_profiles is None or old_config is None:
        rt.say("В архиве pre-install нет profiles.json или config.json.")
        return False
    try:
        cur_config = await ops.read_file(paths.config)
        new_config = _merge_limits(cur_config, old_config)
        old_entries = parse_profiles(old_profiles)
    except (SystemOpsError, ValueError, RenderError) as exc:
        rt.say(f"Не удалось подготовить файлы из pre-install: {_clean(str(exc))}")
        return False
    try:
        cur_entries = parse_profiles(await ops.read_file(paths.profiles))
    except (SystemOpsError, RenderError):
        cur_entries = []
    old_secrets = {base_secret(e.secret) for e in old_entries}
    vanishing = [e for e in cur_entries if base_secret(e.secret) not in old_secrets]
    by_old_name = {e.name: e for e in old_entries}
    users = await rt.ctx.db.run(repo.all_users)
    reissued = sum(
        1
        for u in users
        if u.imported
        and u.source_profile_name in by_old_name
        and base_secret(by_old_name[u.source_profile_name].secret) != base_secret(u.secret)
    )
    rt.say("Что изменится в profiles.json:")
    rt.say(f"  сейчас профилей: {len(cur_entries)}, после восстановления: {len(old_entries)}")
    rt.say(f"  вернутся профили из pre-install: {len(old_entries)}")
    rt.say(
        f"  исчезнут профили, которых не было до установки: {len(vanishing)} "
        "(их ссылки перестанут работать)"
    )
    rt.say(f"  пользователей, у которых секрет вернётся к старому: {reissued}")
    rt.say("В config.json возвращается только блок limits; остальные ключи остаются как есть.")
    if not assume_yes and not _confirm(rt, "Заменить profiles.json так? [y/N] "):
        rt.say("Отменено.")
        return False
    attrs = await _file_attrs(rt)
    if not await _install_proxy_files(rt, old_profiles, new_config, attrs):
        return False
    rt.say("profiles.json возвращён к состоянию pre-install, limits восстановлены, relay работает.")
    return True


async def _rebuild_profiles_for_legacy(rt: Runtime, *, assume_yes: bool) -> bool:
    """--force without an archive: profiles.json rebuilt from the DB, all pointing at 2398."""
    import json

    from tgpanel.domain.secrets_ import generate_secret
    from tgpanel.render.profiles import SENTINEL_NAME

    ops, cfg = rt.ops, rt.config
    paths = cfg.paths
    try:
        doc = json.loads(await ops.read_file(paths.profiles))
        entries = doc["profiles"]
        if not isinstance(entries, list):
            raise ValueError("profiles")
    except (SystemOpsError, ValueError, KeyError, TypeError):
        rt.say("profiles.json не читается: пересборка невозможна.")
        return False
    users = {u.profile_name: u for u in await rt.ctx.db.run(repo.all_users)}
    backend = f"127.0.0.1:{cfg.legacy_port}"
    kept: list[dict[str, Any]] = []
    foreign = 0
    imported = 0
    dropped = 0
    taken = {e.get("name") for e in entries if isinstance(e, dict)}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        ours = (
            name == SENTINEL_NAME
            or name in users
            or str(entry.get("backend", "")).startswith("127.64.")
        )
        if not ours:
            kept.append(entry)
            foreign += 1
            continue
        user = users.get(name)
        if user is not None and user.imported:
            new_name = user.source_profile_name or name
            if new_name != name and new_name in taken:
                new_name = name
            rebuilt = {k: v for k, v in entry.items() if k != "limits"}
            rebuilt.update(name=new_name, backend=backend)
            kept.append(rebuilt)
            imported += 1
        else:
            dropped += 1
    if not kept:  # the relay refuses an empty list
        kept.append(
            {
                "name": SENTINEL_NAME,
                "secret": generate_secret(),
                "backend": backend,
                "carrier_mode": "https",
            }
        )
    rt.say("Профили relay будут пересобраны из базы данных (копии pre-install нет):")
    rt.say(f"  чужие профили сохраняются без изменений: {foreign}")
    rt.say(
        f"  импортированные пользователи уйдут на старый MTProxy и продолжат работать: {imported}"
    )
    rt.say(
        f"  пользователи, созданные уже в панели: {dropped} — их профили удаляются, "
        "ссылки перестанут работать (старый MTProxy не знает их секретов)"
    )
    if not assume_yes and not _confirm(rt, "Продолжить? [y/N] "):
        rt.say("Отменено.")
        return False
    new_profiles = (json.dumps({"profiles": kept}, indent=2, ensure_ascii=False) + "\n").encode()
    attrs = await _file_attrs(rt)
    if not await _install_proxy_files(rt, new_profiles, None, attrs):
        return False
    rt.say("profiles.json пересобран, relay работает; config.json не менялся.")
    return True


async def _remove_our_components(rt: Runtime) -> list[str]:
    ops, cfg = rt.ops, rt.config
    paths = cfg.paths
    problems: list[str] = []

    async def attempt(label: str, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except SystemOpsError as exc:
            problems.append(f"{label}: {_clean(str(exc))}")

    pool_ids: set[int] = set()
    with contextlib.suppress(Exception):
        pool_ids |= {p.id for p in await rt.ctx.db.run(repo.list_pools)}
    with contextlib.suppress(SystemOpsError):
        for name in await ops.list_dir(paths.pools_dir):
            stem, _, suffix = name.partition(".")
            if suffix == "env" and stem.isdigit():
                pool_ids.add(int(stem))
    for pool_id in sorted(pool_ids):
        await attempt(f"пул {pool_id}", ops.systemctl("disable-now", cfg.pool_unit_name(pool_id)))
        await attempt(f"env пула {pool_id}", ops.remove(paths.pool_env(pool_id)))
    owned: list[str] = []
    with contextlib.suppress(SystemOpsError):
        owned = [
            n
            for n in await ops.list_dir(paths.systemd_dir)
            if _OWNED_UNIT_RE.match(n) and "@" not in n
        ]
    for name in owned:
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("disable-now", name)
    await attempt("таблица nft", ops.nft_delete_table("tgpanel"))
    for path in (paths.nft_file, paths.pool_unit):
        await attempt(path, ops.remove(path))
    for name in owned:
        await attempt(name, ops.remove(f"{paths.systemd_dir}/{name}"))
    await attempt("daemon-reload", ops.systemctl("daemon-reload", ""))
    rt.say(f"Удалены пулы ({len(pool_ids)}), юниты tgpanel, таблица nft.")
    return problems


def _panel_url(env: dict[str, str], fallback_domain: str) -> str | None:
    domain = env.get("TGPANEL_PANEL_DOMAIN") or fallback_domain
    path = env.get("TGPANEL_PANEL_PATH", "")
    if not domain or not path:
        return None
    return f"https://{domain}/{path}/"


def _setting(conn: Any, key: str) -> str:
    return str(repo.get_setting(conn, key, "") or "")


async def cmd_show_url(rt: Runtime, args: argparse.Namespace) -> int:
    env = await read_install_env(rt.ops, rt.config)
    domain = ""
    login = ""
    with contextlib.suppress(Exception):
        domain = (await rt.ctx.db.run(_setting, "panel_hostname")) or ""
        login = (await rt.ctx.db.run(_setting, SETTING_LOGIN)) or ""
    url = _panel_url(env, domain)
    if url is None:
        rt.say("Адрес панели неизвестен: нет /etc/tgpanel/tgpanel.env. Выполните tgpanel repair.")
        return EXIT_ERROR
    rt.say(f"Адрес панели: {url}")
    if login:
        rt.say(f"Логин: {login}")
    rt.say("Пароль не хранится в открытом виде; сменить: tgpanel reset-password")
    return EXIT_OK


def generate_password() -> str:
    return secrets.token_urlsafe(15)


def hash_password(password: str) -> str:
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


async def cmd_reset_password(rt: Runtime, args: argparse.Namespace) -> int:
    password = generate_password()
    digest = hash_password(password)
    now = rt.ctx.pipeline.now()

    def write(conn: Any) -> str:
        with transaction(conn):
            login = args.login or repo.get_setting(conn, SETTING_LOGIN, "") or "admin"
            repo.set_setting(conn, SETTING_LOGIN, login)
            repo.set_setting(conn, SETTING_PASSWORD_HASH, digest)
            # invalidates every existing web session (the web layer compares this version)
            version = int(repo.get_setting(conn, SETTING_SESSION_VERSION, "0") or 0) + 1
            repo.set_setting(conn, SETTING_SESSION_VERSION, str(version))
            repo.add_audit(conn, now, ACTOR, "panel.reset_password", "", "")
        return str(login)

    try:
        login = await rt.ctx.pipeline.db_write(write)
    except Exception as exc:
        rt.say(f"Не удалось сохранить пароль: {_clean(str(exc))}")
        return EXIT_ERROR
    rt.say(f"Логин: {login}")
    rt.say(f"Новый пароль: {password}")
    rt.say("Пароль показан один раз и нигде не сохранён. Все прежние сессии панели завершены.")
    return EXIT_OK


async def cmd_bootstrap(rt: Runtime, args: argparse.Namespace) -> int:
    """Internal (install.sh): store login/password hash, host names and the first admin."""
    password = os.environ.get(BOOTSTRAP_PASSWORD_ENV, "")
    caddy_env = await read_caddy_env(rt.ops, rt.config)
    proxy_host = caddy_env.get("TPROXY_HOSTNAME", "")
    digest = hash_password(password) if password else ""
    now = rt.ctx.pipeline.now()

    def write(conn: Any) -> bool:
        created = False
        with transaction(conn):
            if args.domain:
                repo.set_setting(conn, "panel_hostname", args.domain)
            if proxy_host and not repo.get_setting(conn, "proxy_hostname", ""):
                repo.set_setting(conn, "proxy_hostname", proxy_host)
            if args.login and (digest and not repo.get_setting(conn, SETTING_PASSWORD_HASH)):
                repo.set_setting(conn, SETTING_LOGIN, args.login)
                repo.set_setting(conn, SETTING_PASSWORD_HASH, digest)
                created = True
            # the admin is added on the FIRST bootstrap only: later it may be removed in the panel
            if args.admin_id and not repo.get_setting(conn, SETTING_BOOTSTRAPPED):
                repo.add_admin(conn, int(args.admin_id), now)
            repo.set_setting(conn, SETTING_BOOTSTRAPPED, "1")
            repo.add_audit(conn, now, ACTOR, "install.bootstrap", "", "")
        return created

    created = await rt.ctx.pipeline.db_write(write)
    rt.say("credentials-created" if created else "credentials-kept")
    return EXIT_OK


async def cmd_caddy_install(rt: Runtime, args: argparse.Namespace) -> int:
    """Internal (install.sh): block -> validate -> restart -> wait for the certificate."""
    ops = rt.ops
    stored = await stored_cert_exists(ops, args.domain)
    outcome = await install_caddy_block(rt, args.domain, args.path)
    if not outcome.ok:
        rt.say(f"Блок панели в Caddyfile не установлен: {outcome.error}")
        return EXIT_ERROR
    rt.say(
        "Блок панели добавлен, Caddy перезапущен."
        if outcome.changed
        else "Блок панели уже на месте, Caddy не перезапускался."
    )
    rt.say(f"Жду сертификат для {args.domain} (до {args.wait_cert} с)…")
    result = await wait_for_certificate(
        ops,
        args.domain,
        stored_before=stored,
        timeout_s=float(args.wait_cert),
        interval_s=5.0,
        clock=rt.clock,
        sleep=rt.sleep,
        monotonic=rt.monotonic,
    )
    if result.cert is not None:
        origin = {
            True: "использован существующий сертификат",
            False: "сертификат выпущен только что",
            None: "сертификат получен",
        }[result.reused]
        rt.say(f"Сертификат: {origin}; {describe_cert(result.cert, rt.clock())}")
        return EXIT_OK
    rt.say("Сертификат за отведённое время не получен.")
    with contextlib.suppress(SystemOpsError):
        tail = await rt.tools.journal_tail("caddy", 30)
        if tail.strip():
            rt.say("Последние строки журнала Caddy:")
            rt.say(tail)
    rt.say(
        "Caddy продолжит попытки сам; панель заработает без переустановки, как только "
        "причина будет устранена (DNS, доступность порта 80, лимиты Let's Encrypt). "
        "Прокси это не затронуло."
    )
    return EXIT_PENDING


async def cmd_pre_install_backup(rt: Runtime, args: argparse.Namespace) -> int:
    """Internal (install.sh): the never-rotated `pre-install` archive (created once)."""
    ops, paths = rt.ops, rt.config.paths
    await ops.ensure_dir(paths.backups_dir, 0o700, "root", "root")
    existing = [b for b in await scan_disk_backups(ops, paths) if b.reason == PRE_INSTALL]
    if existing:
        first = min(existing, key=lambda b: b.created_at)
        rt.say(first.path)
        return EXIT_OK
    try:
        info = await backup_mod.create_backup(
            ops, paths, reason=PRE_INSTALL, now=rt.clock().replace(microsecond=0), db_file=None
        )
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    rt.say(info.path)
    return EXIT_OK


async def cmd_migrate(rt: Runtime, args: argparse.Namespace) -> int:
    rt.say(f"schema version {await rt.ctx.db.run(current_version)}")
    return EXIT_OK


# ------------------------------------------------------------------------ registration

_Handler = Callable[[Runtime, argparse.Namespace], Awaitable[int]]
_HANDLERS: dict[str, _Handler] = {
    "doctor": cmd_doctor,
    "repair": cmd_repair,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
    "show-url": cmd_show_url,
    "reset-password": cmd_reset_password,
    "caddy-install": cmd_caddy_install,
    "bootstrap": cmd_bootstrap,
    "pre-install-backup": cmd_pre_install_backup,
    "migrate": cmd_migrate,
    "post-update": cmd_post_update,
    "install-units": cmd_install_units,
}
INTERNAL_COMMANDS = (
    "caddy-install",
    "bootstrap",
    "pre-install-backup",
    "migrate",
    "post-update",
    "install-units",
)


def add_commands(sub: Any) -> None:
    sub.add_parser("doctor", help="диагностика всех компонентов")
    sub.add_parser("repair", help="вернуть блок Caddy, юниты и таблицу nft")
    p = sub.add_parser("update", help="обновить tgpanel до ветки, тега или коммита")
    p.add_argument("--ref", help="ветка, тег или коммит (по умолчанию как при установке)")
    p.add_argument("--force", action="store_true", help="переустановить, даже если версия та же")
    p.add_argument(
        "--allow-downgrade", action="store_true", help="разрешить установку более старой версии"
    )
    p.add_argument(
        "--verify-tag",
        action="store_true",
        help="проверить подпись тега по deploy/trusted-signers (или TGPANEL_VERIFY_TAG=1)",
    )
    p = sub.add_parser("uninstall", help="убрать tgpanel и вернуть прокси к состоянию pre-install")
    p.add_argument("--purge", action="store_true", help="удалить также данные и настройки")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p.add_argument("--force", action="store_true", help="удалить, даже если копии pre-install нет")
    sub.add_parser("show-url", help="показать адрес панели")
    p = sub.add_parser("reset-password", help="выдать новый пароль панели")
    p.add_argument("--login", help="сменить и логин")
    p = sub.add_parser("caddy-install", help=argparse.SUPPRESS)
    p.add_argument("--domain", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--wait-cert", type=int, default=120)
    p = sub.add_parser("bootstrap", help=argparse.SUPPRESS)
    p.add_argument("--domain", default="")
    p.add_argument("--login", default="")
    p.add_argument("--admin-id", default="")
    sub.add_parser("pre-install-backup", help=argparse.SUPPRESS)
    sub.add_parser("migrate", help=argparse.SUPPRESS)
    sub.add_parser("install-units", help=argparse.SUPPRESS)
    p = sub.add_parser("post-update", help=argparse.SUPPRESS)
    p.add_argument("--from", dest="old", default="")
    p.add_argument("--to", dest="new", default="")


def _runner(
    name: str,
    ctx_factory: Callable[[], AppContext],
    tools_factory: Callable[[], ShellTools],
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], datetime],
    monotonic: Callable[[], float],
    install_dir: str,
) -> Callable[..., int]:
    def run(
        args: argparse.Namespace,
        out: TextIO | None = None,
        input_fn: Callable[[str], str] = input,
    ) -> int:
        out_stream = out or sys.stdout
        try:
            ctx = ctx_factory()
        except SchemaTooNewError as exc:
            print(str(exc), file=out_stream)
            return EXIT_USAGE
        db_path = getattr(args, "db", None) or os.environ.get("TGPANEL_DB") or DEFAULT_DB
        rt = Runtime(
            ctx=ctx,
            tools=tools_factory(),
            out=out or sys.stdout,
            input_fn=input_fn,
            db_path=db_path,
            sleep=sleep,
            clock=clock,
            monotonic=monotonic,
            install_dir=install_dir,
        )
        try:
            return asyncio.run(_run_handler(name, rt, args))
        except SystemOpsError as exc:
            print(f"Ошибка системы: {_clean(str(exc))}", file=rt.out)
            return EXIT_ERROR
        finally:
            ctx.close()

    return run


async def _run_handler(name: str, rt: Runtime, args: argparse.Namespace) -> int:
    if name not in ("doctor", "migrate"):
        with contextlib.suppress(SystemOpsError):  # e.g. normal user: dirs may be unavailable
            await rt.ctx.start(recover=False)
    return await _HANDLERS[name](rt, args)


def register(
    subparsers: Any,
    ctx_factory: Callable[[], AppContext],
    *,
    tools_factory: Callable[[], ShellTools] = RealShellTools,
    include_internal: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    install_dir: str = INSTALL_DIR,
) -> None:
    """Add the subcommands; each parser gets ``ops_run(args, out, input_fn) -> exit code``."""
    add_commands(subparsers)
    for name, parser in subparsers.choices.items():
        if name in _HANDLERS and (include_internal or name not in INTERNAL_COMMANDS):
            parser.set_defaults(
                ops_run=_runner(
                    name, ctx_factory, tools_factory, sleep, clock, monotonic, install_dir
                )
            )


def main(
    argv: Sequence[str] | None = None,
    *,
    ops: SystemOps | None = None,
    tools: ShellTools | None = None,
    config: ApplyConfig | None = None,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    install_dir: str = INSTALL_DIR,
) -> int:
    from tgpanel.services.container import build_context

    parser = argparse.ArgumentParser(prog="python -m tgpanel.ops_cli")
    parser.add_argument("--db", help="путь к базе SQLite (или TGPANEL_DB)")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--db")
    args_probe, _ = pre.parse_known_args(argv)
    db_path = args_probe.db or os.environ.get("TGPANEL_DB") or DEFAULT_DB
    system: SystemOps
    if ops is None:
        from tgpanel.system.real import RealSystemOps

        system = RealSystemOps()
    else:
        system = ops
    cfg = config or ApplyConfig()
    register(
        sub,
        lambda: build_context(system, db_path, config=cfg),
        tools_factory=(lambda: tools) if tools is not None else RealShellTools,
        sleep=sleep,
        clock=clock,
        monotonic=monotonic,
        install_dir=install_dir,
    )
    args = parser.parse_args(argv)
    args.db = db_path
    return int(args.ops_run(args, out, input_fn))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
