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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from tgpanel.apply import backup as backup_mod
from tgpanel.apply.backup import PRE_INSTALL, BackupError, scan_disk_backups
from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.errors import OperationRejected
from tgpanel.db import repo
from tgpanel.db.connection import current_version, transaction
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
FIREWALL_UNIT = "tgpanel-firewall"
REFRESH_PATH_UNIT = "tgpanel-mtproxy-refresh.path"
POOL_TEMPLATE = "tgpanel-mtproxy@.service"
BOOTSTRAP_PASSWORD_ENV = "TGPANEL_BOOTSTRAP_PASSWORD"  # noqa: S105 - variable name
SETTING_LOGIN = "panel_login"
SETTING_PASSWORD_HASH = "panel_password_hash"  # noqa: S105 - setting name

# deploy file (relative to the repository) -> (target path, mode)
_STATIC_UNITS: tuple[tuple[str, str, int], ...] = (
    ("deploy/tgpanel.service", "/etc/systemd/system/tgpanel.service", 0o644),
    (
        "deploy/tgpanel-mtproxy-refresh.path",
        "/etc/systemd/system/tgpanel-mtproxy-refresh.path",
        0o644,
    ),
    (
        "deploy/tgpanel-mtproxy-refresh.service",
        "/etc/systemd/system/tgpanel-mtproxy-refresh.service",
        0o644,
    ),
    ("deploy/tgpanel-cli", CLI_WRAPPER, 0o755),
)
_FIREWALL_UNIT_PATH = "/etc/systemd/system/tgpanel-firewall.service"
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
    """Unit files and the CLI wrapper: path -> (content, mode). Raises SystemOpsError."""
    files: dict[str, tuple[bytes, int]] = {}
    for rel, target, mode in _STATIC_UNITS:
        files[target] = (await rt.ops.read_file(f"{rt.install_dir}/{rel}"), mode)
    files[_FIREWALL_UNIT_PATH] = (render_firewall_unit(), 0o644)
    return files


async def sync_static_files(rt: Runtime) -> list[str]:
    """Write changed unit files; on any failure restore the previous ones. Returns changed paths."""
    ops = rt.ops
    wanted = await desired_static_files(rt)
    changed: list[tuple[str, bytes | None]] = []
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
        if any(t.startswith("/etc/systemd/system/") for t, _ in changed):
            await ops.systemctl("daemon-reload", "")
    except SystemOpsError:
        for target, previous in reversed(changed):
            with contextlib.suppress(SystemOpsError):
                if previous is None:
                    await ops.remove(target)
                else:
                    mode = wanted[target][1]
                    await ops.write_atomic(target, previous, mode=mode, owner="root", group="root")
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("daemon-reload", "")
        raise
    return [t for t, _ in changed]


async def enable_static_units(rt: Runtime) -> None:
    ops = rt.ops
    await ops.systemctl("enable", FIREWALL_UNIT)
    await ops.systemctl("enable", PANEL_UNIT)
    await ops.systemctl("enable-now", REFRESH_PATH_UNIT)


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


async def _read_state_ref(rt: Runtime) -> str:
    try:
        text = (await rt.ops.read_file(f"{rt.config.paths.state_dir}/ref")).decode().strip()
    except SystemOpsError:
        return DEFAULT_REF
    return text if _REF_RE.fullmatch(text) else DEFAULT_REF


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


async def cmd_update(rt: Runtime, args: argparse.Namespace) -> int:
    ops, tools = rt.ops, rt.tools
    ref = args.ref or await _read_state_ref(rt)
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        rt.say("Некорректное имя ветки, тега или коммита.")
        return EXIT_USAGE
    try:
        previous = await tools.git_head(rt.install_dir)
        await tools.git_fetch(rt.install_dir)
        target = await tools.git_resolve(rt.install_dir, ref)
    except SystemOpsError as exc:
        rt.say(f"Не удалось получить обновление: {_clean(str(exc))}")
        return EXIT_ERROR
    if target == previous and not args.force:
        rt.say(f"Уже установлена версия {previous[:10]} ({ref}); обновлять нечего.")
        return EXIT_OK
    rt.say(f"Обновление {previous[:10]} → {target[:10]} ({ref})")
    try:
        info = await rt.ctx.pipeline.create_backup("pre-update", ACTOR)
        rt.say(f"Резервная копия: {info.path}")
    except BackupError as exc:
        rt.say(f"Резервная копия не создана, обновление остановлено: {exc}")
        return EXIT_ERROR

    async def roll_back(reason: str) -> int:
        rt.say(f"Ошибка: {reason}. Возвращаю версию {previous[:10]}…")
        try:
            await _switch_code(rt, previous)
            await ops.systemctl("restart", PANEL_UNIT)
            if await _panel_healthy(rt):
                rt.say(
                    "Предыдущая версия запущена. Схема БД могла уйти вперёд: "
                    "резервная копия сделана перед обновлением."
                )
            else:
                rt.say("Предыдущая версия возвращена, но панель не отвечает: tgpanel doctor.")
        except SystemOpsError as exc:
            rt.say(f"Откат не удался: {_clean(str(exc))}")
        return EXIT_ERROR

    try:
        await _switch_code(rt, target)
        await rt.tools.migrate_database(rt.venv_python, rt.install_dir, rt.db_path)
        changed = await sync_static_files(rt)
        await enable_static_units(rt)
        if changed:
            rt.say("Обновлены файлы: " + ", ".join(p.rsplit("/", 1)[-1] for p in changed))
        await ops.systemctl("restart", PANEL_UNIT)
    except SystemOpsError as exc:
        return await roll_back(_clean(str(exc)))
    if not await _panel_healthy(rt):
        return await roll_back("панель не запустилась после обновления")
    with contextlib.suppress(SystemOpsError):
        await ops.write_atomic(
            f"{rt.config.paths.state_dir}/ref",
            f"{ref}\n".encode(),
            mode=0o600,
            owner="root",
            group="root",
        )
    rt.say("Обновление выполнено, панель работает.")
    return EXIT_OK


async def cmd_uninstall(rt: Runtime, args: argparse.Namespace) -> int:
    ops, cfg = rt.ops, rt.config
    paths = cfg.paths
    rt.say("Удаление tgpanel возвращает прокси к состоянию до установки панели.")
    rt.say("Пользователи, созданные панелью, перестанут работать; /var/lib/caddy не трогается.")
    if args.purge:
        rt.say("ВНИМАНИЕ: --purge удалит базу данных, настройки и каталог /opt/tgpanel.")
    if not args.yes and not _confirm(rt, "Продолжить? [y/N] "):
        rt.say("Отменено.")
        return EXIT_ERROR

    archives = [b for b in await scan_disk_backups(ops, paths) if b.reason == PRE_INSTALL]
    if not archives:
        rt.say("Копия pre-install не найдена: профили прокси восстановить не из чего.")
        if not args.yes and not _confirm(rt, "Удалить панель без восстановления профилей? [y/N] "):
            rt.say("Отменено.")
            return EXIT_ERROR
    archive = min(archives, key=lambda b: b.created_at) if archives else None

    # 0. stop the panel first: nothing may re-apply while we take things apart
    with contextlib.suppress(SystemOpsError):
        await ops.systemctl("disable-now", PANEL_UNIT)
    try:
        info = await rt.ctx.pipeline.create_backup("pre-uninstall", ACTOR)
        rt.say(f"Резервная копия перед удалением: {info.path}")
    except BackupError as exc:
        rt.say(f"Не удалось создать резервную копию: {exc}")
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("enable-now", PANEL_UNIT)
        return EXIT_ERROR

    # 1. proxy files back to pre-install (relay -> the legacy MTProxy again)
    if archive is not None:
        ok = await _restore_proxy_files(rt, archive.path)
        if not ok:
            with contextlib.suppress(SystemOpsError):
                await ops.systemctl("enable-now", PANEL_UNIT)
            return EXIT_ERROR

    # 2. our units, pools, nft table
    problems = await _remove_our_components(rt)

    # 3. the Caddy block (proxy block and /var/lib/caddy stay untouched)
    outcome = await remove_caddy_block(rt)
    if not outcome.ok:
        problems.append(f"блок Caddy не удалён: {outcome.error}")
    elif outcome.changed:
        rt.say("Блок панели удалён из Caddyfile, Caddy перезапущен.")

    # 4. data
    if args.purge:
        for tree in sorted(REMOVABLE_TREES):
            try:
                await rt.tools.remove_tree(tree)
            except SystemOpsError as exc:
                problems.append(f"{tree}: {_clean(str(exc))}")
        with contextlib.suppress(SystemOpsError):
            await ops.remove(CLI_WRAPPER)
        rt.say(f"Данные удалены. Резервные копии остались в {paths.backups_dir}.")
    else:
        rt.say(
            f"Данные сохранены: база {DEFAULT_DB}, настройки {paths.tgpanel_dir}, резервные копии."
        )
    for problem in problems:
        rt.say(f"Предупреждение: {problem}")
    rt.say("Готово." if not problems else "Удаление завершено с предупреждениями.")
    return EXIT_OK if not problems else EXIT_ERROR


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


async def _restore_proxy_files(rt: Runtime, archive_path: str) -> bool:
    """profiles.json + config.json from the pre-install archive; checked, rolled back on failure."""
    import json

    ops, cfg = rt.ops, rt.config
    paths = cfg.paths
    try:
        members = await ops.read_tar_members(
            archive_path, {backup_mod.MANIFEST_NAME, *_RESTORE_MEMBERS}
        )
    except SystemOpsError as exc:
        rt.say(f"Архив pre-install не читается: {_clean(str(exc))}")
        return False
    try:
        meta = json.loads(members[backup_mod.MANIFEST_NAME]).get("files", {})
    except (KeyError, ValueError):
        rt.say("В архиве pre-install повреждён MANIFEST.json.")
        return False
    new_profiles = members.get(_RESTORE_MEMBERS[0])
    new_config = members.get(_RESTORE_MEMBERS[1])
    if new_profiles is None or new_config is None:
        rt.say("В архиве pre-install нет profiles.json или config.json.")
        return False
    # the legacy MTProxy must run again before the relay points at it
    try:
        text = await rt.ctx.pipeline.set_legacy_mtproxy(True, ACTOR)
        rt.say(text)
        if not await ops.wait_tcp_open("127.0.0.1", cfg.legacy_port, cfg.timing.port_timeout_s):
            rt.say(f"Старый MTProxy не открыл порт {cfg.legacy_port}; восстановление остановлено.")
            return False
    except (OperationRejected, SystemOpsError) as exc:
        rt.say(f"Не удалось включить старый MTProxy: {_clean(str(exc))}")
        return False

    def attrs(path: str) -> tuple[int, str, str]:
        info = meta.get(path.lstrip("/"), {})
        return (
            int(info.get("mode", 0o400)),
            str(info.get("owner", "root")),
            str(info.get("group", cfg.tproxy_group)),
        )

    pm, po, pg = attrs(paths.profiles)
    cm, co, cg = attrs(paths.config)
    try:
        before = {p: await ops.read_file(p) for p in (paths.profiles, paths.config)}
        await ops.write_atomic(paths.check_profiles, new_profiles, mode=0o600, owner=po, group=pg)
        await ops.write_atomic(paths.check_config, new_config, mode=0o600, owner=co, group=cg)
        try:
            verdict = await ops.tproxy_check(paths.check_config, paths.check_profiles)
        finally:
            for tmp in (paths.check_profiles, paths.check_config):
                with contextlib.suppress(SystemOpsError):
                    await ops.remove(tmp)
    except SystemOpsError as exc:
        rt.say(f"Проверка восстановленных файлов не выполнена: {_clean(str(exc))}")
        return False
    if not verdict.ok:
        rt.say(f"relay отклонил файлы из pre-install: {verdict.output}")
        return False
    try:
        await ops.write_atomic(paths.config, new_config, mode=cm, owner=co, group=cg)
        await ops.write_atomic(paths.profiles, new_profiles, mode=pm, owner=po, group=pg)
        await ops.systemctl("restart", cfg.relay_unit)
        healthy = await _wait_healthz(rt)
    except SystemOpsError as exc:
        healthy = False
        rt.say(f"Ошибка при восстановлении: {_clean(str(exc))}")
    if not healthy:
        rt.say("relay не ответил после восстановления профилей; возвращаю прежние файлы.")
        with contextlib.suppress(SystemOpsError):
            await ops.write_atomic(paths.config, before[paths.config], mode=cm, owner=co, group=cg)
            await ops.write_atomic(
                paths.profiles, before[paths.profiles], mode=pm, owner=po, group=pg
            )
            await ops.systemctl("restart", cfg.relay_unit)
        return False
    rt.say("profiles.json и config.json восстановлены из pre-install, relay работает.")
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
    for unit in (REFRESH_PATH_UNIT, "tgpanel-mtproxy-refresh.service", FIREWALL_UNIT, PANEL_UNIT):
        with contextlib.suppress(SystemOpsError):
            await ops.systemctl("disable-now", unit)
    await attempt("таблица nft", ops.nft_delete_table("tgpanel"))
    for path in (
        paths.nft_file,
        paths.pool_unit,
        _FIREWALL_UNIT_PATH,
        "/etc/systemd/system/tgpanel.service",
        "/etc/systemd/system/tgpanel-mtproxy-refresh.path",
        "/etc/systemd/system/tgpanel-mtproxy-refresh.service",
    ):
        await attempt(path, ops.remove(path))
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
            repo.add_audit(conn, now, ACTOR, "panel.reset_password", "", "")
        return str(login)

    try:
        login = await rt.ctx.pipeline.db_write(write)
    except Exception as exc:
        rt.say(f"Не удалось сохранить пароль: {_clean(str(exc))}")
        return EXIT_ERROR
    rt.say(f"Логин: {login}")
    rt.say(f"Новый пароль: {password}")
    rt.say(
        "Пароль показан один раз и нигде не сохранён. Действующие сессии панели нужно завершить."
    )
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
            if args.admin_id:
                repo.add_admin(conn, int(args.admin_id), now)
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
}
INTERNAL_COMMANDS = ("caddy-install", "bootstrap", "pre-install-backup", "migrate")


def add_commands(sub: Any) -> None:
    sub.add_parser("doctor", help="диагностика всех компонентов")
    sub.add_parser("repair", help="вернуть блок Caddy, юниты и таблицу nft")
    p = sub.add_parser("update", help="обновить tgpanel до ветки, тега или коммита")
    p.add_argument("--ref", help="ветка, тег или коммит (по умолчанию как при установке)")
    p.add_argument("--force", action="store_true", help="переустановить, даже если версия та же")
    p = sub.add_parser("uninstall", help="убрать tgpanel и вернуть прокси к состоянию pre-install")
    p.add_argument("--purge", action="store_true", help="удалить также данные и настройки")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
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


def _runner(
    name: str,
    ctx_factory: Callable[[], AppContext],
    tools_factory: Callable[[], ShellTools],
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], datetime],
) -> Callable[..., int]:
    def run(
        args: argparse.Namespace,
        out: TextIO | None = None,
        input_fn: Callable[[str], str] = input,
    ) -> int:
        ctx = ctx_factory()
        db_path = getattr(args, "db", None) or os.environ.get("TGPANEL_DB") or DEFAULT_DB
        rt = Runtime(
            ctx=ctx,
            tools=tools_factory(),
            out=out or sys.stdout,
            input_fn=input_fn,
            db_path=db_path,
            sleep=sleep,
            clock=clock,
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
) -> None:
    """Add the subcommands; each parser gets ``ops_run(args, out, input_fn) -> exit code``."""
    add_commands(subparsers)
    for name, parser in subparsers.choices.items():
        if name in _HANDLERS and (include_internal or name not in INTERNAL_COMMANDS):
            parser.set_defaults(ops_run=_runner(name, ctx_factory, tools_factory, sleep, clock))


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
    )
    args = parser.parse_args(argv)
    args.db = db_path
    return int(args.ops_run(args, out, input_fn))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
