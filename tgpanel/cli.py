"""tgpanel command line interface.

Implemented here: status, apply, import, backup, restore, legacy-mtproxy. The operations
commands (doctor, repair, update, uninstall, show-url, reset-password and the internal helpers
used by install.sh) live in ``ops_cli`` and are registered into the same parser by ``main``.

User-facing output is Russian. ``main`` accepts a SystemOps so tests run on FakeSystemOps.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from tgpanel import ops_cli
from tgpanel.apply.backup import BackupError
from tgpanel.apply.config import ApplyConfig, ApplyPaths
from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.importer import ImportPreview, ImportSourceError
from tgpanel.db import repo
from tgpanel.domain.import_ import DEFAULT_ID_REGEX
from tgpanel.domain.models import UserStatus
from tgpanel.services.container import AppContext, build_context
from tgpanel.system.ops import SystemOps, SystemOpsError
from tgpanel.system.tools import RealShellTools, ShellTools

DEFAULT_DB = "/var/lib/tgpanel/tgpanel.db"
ACTOR = "system"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_EXTERNAL = 3


def build_parser(
    ctx_factory: Callable[[], AppContext] | None = None,
    *,
    tools_factory: Callable[[], ShellTools] = RealShellTools,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgpanel", description="Панель управления tproxy-server")
    parser.add_argument("--db", help="путь к базе SQLite (или переменная TGPANEL_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="состояние сервисов, пользователей и последнего применения")

    p_apply = sub.add_parser("apply", help="пересобрать конфигурацию прокси из БД и применить")
    p_apply.add_argument(
        "--force-external",
        action="store_true",
        help="перезаписать profiles.json, изменённый вне панели",
    )
    p_apply.add_argument(
        "--reload-nft", action="store_true", help="полностью перезагрузить таблицу nft"
    )
    p_apply.add_argument(
        "--adopt",
        action="store_true",
        help="принять текущий profiles.json как базовый (ничего не применять)",
    )
    p_apply.add_argument(
        "--prune-orphans",
        action="store_true",
        help="остановить и удалить пулы, неизвестные базе данных",
    )

    p_imp = sub.add_parser("import", help="импорт существующих профилей прокси")
    p_imp.add_argument("--dry-run", action="store_true", help="только показать план")
    p_imp.add_argument("--csv", help="CSV: имя_профиля;telegram_id;имя;комментарий")
    p_imp.add_argument("--id-regex", default=DEFAULT_ID_REGEX, help="выражение для Telegram ID")
    p_imp.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")

    p_bak = sub.add_parser("backup", help="создать резервную копию или показать список")
    p_bak.add_argument("--reason", default="manual", help="причина (часть имени файла)")
    p_bak.add_argument("--list", action="store_true", help="показать список копий")

    p_res = sub.add_parser("restore", help="восстановить состояние из резервной копии")
    p_res.add_argument("file", help="путь к архиву .tar.gz")
    p_res.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")

    p_leg = sub.add_parser("legacy-mtproxy", help="включить/выключить старый процесс MTProxy")
    p_leg.add_argument("state", choices=["on", "off"])
    p_leg.add_argument("--force", action="store_true", help="выключить, даже если он используется")
    if ctx_factory is not None:
        ops_cli.register(
            sub,
            ctx_factory,
            tools_factory=tools_factory,
            sleep=sleep,
            clock=clock,
            monotonic=monotonic,
        )
    return parser


def resolve_db_path(arg: str | None) -> str:
    return arg or os.environ.get("TGPANEL_DB") or DEFAULT_DB


def config_from_env() -> ApplyConfig:
    paths = ApplyPaths()
    lock = os.environ.get("TGPANEL_LOCK")
    backups = os.environ.get("TGPANEL_BACKUP_DIR")
    if lock:
        paths = replace(paths, lock=lock)
    if backups:
        paths = replace(paths, backups_dir=backups)
    return ApplyConfig(paths=paths)


def _fmt_dt(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S UTC")


# ----------------------------------------------------------------------------- commands


async def _cmd_status(ctx: AppContext, out: TextIO) -> int:
    ops = ctx.pipeline.ops
    cfg = ctx.pipeline.config
    users = await ctx.db.run(repo.all_users)
    pools = await ctx.db.run(repo.list_pools)
    counts = {s: sum(1 for u in users if u.status is s) for s in UserStatus}
    print("Состояние tgpanel", file=out)
    print("", file=out)
    print("Сервисы:", file=out)
    units = [cfg.relay_unit, "caddy", "mtproxy", *[cfg.pool_unit_name(p.id) for p in pools]]
    for unit in units:
        try:
            active = await ops.is_active(unit)
        except SystemOpsError:
            active = False
        label = "старый MTProxy" if unit == "mtproxy" else unit
        print(f"  {label}: {'работает' if active else 'не запущен'}", file=out)
    health = await ops.http_get(f"{cfg.admin_url}/healthz", cfg.timing.http_timeout_s)
    healthy = health.status == 200
    print(f"  relay /healthz: {'ok' if healthy else 'нет ответа'}", file=out)
    print("", file=out)
    print(
        f"Пользователи: всего {len(users)} (активных {counts[UserStatus.ACTIVE]}, "
        f"отключённых {counts[UserStatus.DISABLED]}, истёкших {counts[UserStatus.EXPIRED]})",
        file=out,
    )
    occupancy = await ctx.db.run(repo.pool_occupancy)
    spp = (await ctx.settings.snapshot()).secrets_per_process
    for pool in pools:
        print(
            f"  пул {pool.id}: порт {pool.port}, секретов {occupancy.get(pool.id, 0)} из {spp}",
            file=out,
        )
    runs = await ctx.db.run(repo.list_apply_runs, 1)
    print("", file=out)
    if runs:
        run = runs[0]
        extra = f", ошибка: {run.error}" if run.error else ""
        print(
            f"Последнее применение: #{run.id} {run.status} ({run.reason}) "
            f"{_fmt_dt(run.started_at)}{extra}",
            file=out,
        )
    else:
        print("Применений ещё не было", file=out)
    drift = await ctx.pipeline.detect_drift()
    if drift is None:
        print("profiles.json: изменений вне панели нет", file=out)
    else:
        print(f"profiles.json: ИЗМЕНЁН ВНЕ ПАНЕЛИ — {drift.description}", file=out)
    backups = await ctx.db.run(repo.list_backups)
    last = backups[0].created_at if backups else None
    print(f"Резервных копий: {len(backups)}, последняя: {_fmt_dt(last)}", file=out)
    return EXIT_OK if healthy else EXIT_ERROR


async def _cmd_apply(ctx: AppContext, args: argparse.Namespace, out: TextIO) -> int:
    if args.adopt:
        print(await ctx.pipeline.adopt(ACTOR), file=out)
        return EXIT_OK
    if args.prune_orphans:
        pruned = await ctx.pipeline.prune_orphans(ACTOR)
        if not pruned.ok:
            print(pruned.error or "Не удалось удалить лишние пулы", file=out)
            return EXIT_ERROR
        print("Лишние пулы удалены.", file=out)
        return EXIT_OK
    print("Применяю конфигурацию…", file=out)
    outcome = await ctx.pipeline.apply_now(
        "cli-apply",
        ACTOR,
        force_external=args.force_external,
        full_nft_reload=args.reload_nft,
    )
    if outcome.status == "needs_adoption":
        print(outcome.error, file=out)
        return EXIT_EXTERNAL
    if outcome.status == "external_change":
        print(outcome.error or "profiles.json изменён вне панели", file=out)
        print(
            "Импортируйте новые профили (tgpanel import) или добавьте --force-external.", file=out
        )
        return EXIT_EXTERNAL
    if not outcome.ok:
        print(outcome.error or "Не удалось применить изменения", file=out)
        return EXIT_ERROR
    if outcome.status == "noop":
        print("Изменений нет: система уже соответствует базе данных.", file=out)
    else:
        print(f"Готово (применение #{outcome.apply_run_id}).", file=out)
    for warning in outcome.warnings:
        print(f"Предупреждение: {warning}", file=out)
    return EXIT_OK


def _print_preview(preview: ImportPreview, out: TextIO) -> None:
    print("Исходный профиль → Telegram ID → имя → пул → адрес", file=out)
    for pr in preview.rows:
        row = pr.row
        if not row.will_import:
            print(f"  {row.source_name}: пропущен ({row.skip_reason})", file=out)
            continue
        tg = row.tg_id if row.tg_id is not None else "—"
        new = " (новый пул)" if pr.new_pool else ""
        print(
            f"  {row.source_name} → {tg} → {row.display_name} → пул {pr.pool_id}{new}"
            f" (порт {pr.pool_port}) → {pr.loopback_ip}",
            file=out,
        )
    print(f"К импорту: {preview.importable} из {len(preview.rows)}", file=out)
    for warning in preview.warnings:
        print(f"Предупреждение: {warning}", file=out)
    for error in preview.errors:
        print(f"ОШИБКА: {error}", file=out)


async def _cmd_import(
    ctx: AppContext, args: argparse.Namespace, out: TextIO, input_fn: Callable[[str], str]
) -> int:
    csv_text: str | None = None
    if args.csv:
        try:
            csv_text = await asyncio.to_thread(Path(args.csv).read_text, encoding="utf-8")
        except OSError:
            print(f"Не удалось прочитать CSV-файл: {args.csv}", file=out)
            return EXIT_USAGE
    try:
        preview = await ctx.importer.preview(csv_text=csv_text, id_regex=args.id_regex)
    except ImportSourceError as exc:
        print(str(exc), file=out)
        return EXIT_ERROR
    _print_preview(preview, out)
    if preview.blocked:
        print("Импорт заблокирован: исправьте ошибки выше.", file=out)
        return EXIT_ERROR
    if args.dry_run:
        return EXIT_OK
    if preview.importable == 0:
        print("Импортировать нечего.", file=out)
        return EXIT_OK
    if not args.yes:
        answer = input_fn("Остановлен ли прежний бот? Выполнить импорт? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "д", "да"):
            print("Отменено.", file=out)
            return EXIT_ERROR
    result = await ctx.importer.confirm(preview, actor=ACTOR)
    if not result.ok:
        print(f"Импорт не выполнен: {result.error}", file=out)
        return EXIT_ERROR
    print(f"Импортировано пользователей: {result.imported}", file=out)
    print("Старый процесс MTProxy можно отключить командой: tgpanel legacy-mtproxy off", file=out)
    return EXIT_OK


async def _cmd_backup(ctx: AppContext, args: argparse.Namespace, out: TextIO) -> int:
    if args.list:
        await ctx.pipeline.sync_backups()
        for rec in await ctx.db.run(repo.list_backups):
            print(
                f"{_fmt_dt(rec.created_at)}  {rec.reason:<14} {rec.size:>10}  {rec.path}", file=out
            )
        return EXIT_OK
    try:
        info = await ctx.pipeline.create_backup(args.reason, ACTOR)
    except BackupError as exc:
        print(str(exc), file=out)
        return EXIT_ERROR
    print(f"Резервная копия создана: {info.path} ({info.size} байт)", file=out)
    return EXIT_OK


async def _cmd_restore(
    ctx: AppContext, args: argparse.Namespace, out: TextIO, input_fn: Callable[[str], str]
) -> int:
    if not args.yes:
        answer = (
            input_fn("Состояние БД и прокси будет заменено содержимым архива. Продолжить? [y/N] ")
            .strip()
            .lower()
        )
        if answer not in ("y", "yes", "д", "да"):
            print("Отменено.", file=out)
            return EXIT_ERROR
    outcome = await ctx.pipeline.restore_backup(args.file, ACTOR, allow_external_path=True)
    if not outcome.ok:
        print(f"Восстановление не выполнено: {outcome.error}", file=out)
        return EXIT_ERROR
    print("Состояние восстановлено из резервной копии.", file=out)
    return EXIT_OK


async def _cmd_legacy(ctx: AppContext, args: argparse.Namespace, out: TextIO) -> int:
    try:
        text = await ctx.importer.legacy_mtproxy(args.state == "on", ACTOR, force=args.force)
    except OperationRejected as exc:
        print(str(exc), file=out)
        return EXIT_ERROR
    print(text, file=out)
    return EXIT_OK


# --------------------------------------------------------------------------------- main


def main(
    argv: Sequence[str] | None = None,
    *,
    ops: SystemOps | None = None,
    config: ApplyConfig | None = None,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
    tools: ShellTools | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    stream = out or sys.stdout
    cfg = config or config_from_env()
    holder: dict[str, SystemOps] = {}

    def system() -> SystemOps:
        if ops is not None:
            return ops
        if "ops" not in holder:
            from tgpanel.system.real import RealSystemOps

            holder["ops"] = RealSystemOps()
        return holder["ops"]

    db_arg: list[str] = []

    def ops_context() -> AppContext:
        return build_context(system(), resolve_db_path(db_arg[0] if db_arg else None), config=cfg)

    parser = build_parser(
        ops_context,
        tools_factory=(lambda: tools) if tools is not None else RealShellTools,
        sleep=sleep,
        clock=clock,
        monotonic=monotonic,
    )
    args = parser.parse_args(argv)
    db_path = resolve_db_path(args.db)
    if getattr(args, "ops_run", None) is not None:
        db_arg.append(db_path)
        args.db = db_path
        return int(args.ops_run(args, stream, input_fn))
    ctx = build_context(system(), db_path, config=cfg)
    try:
        return asyncio.run(_run(ctx, args, stream, input_fn))
    except SystemOpsError as exc:
        print(f"Ошибка системы: {exc}", file=stream)
        return EXIT_ERROR
    finally:
        ctx.close()


async def _run(
    ctx: AppContext, args: argparse.Namespace, out: TextIO, input_fn: Callable[[str], str]
) -> int:
    with contextlib.suppress(SystemOpsError):  # best effort (e.g. `status` as a normal user)
        await ctx.start(recover=False)
    return await _dispatch(ctx, args, out, input_fn)


async def _dispatch(
    ctx: AppContext, args: argparse.Namespace, out: TextIO, input_fn: Callable[[str], str]
) -> int:
    cmd = args.command
    if cmd == "status":
        return await _cmd_status(ctx, out)
    if cmd == "apply":
        return await _cmd_apply(ctx, args, out)
    if cmd == "import":
        return await _cmd_import(ctx, args, out, input_fn)
    if cmd == "backup":
        return await _cmd_backup(ctx, args, out)
    if cmd == "restore":
        return await _cmd_restore(ctx, args, out, input_fn)
    if cmd == "legacy-mtproxy":
        return await _cmd_legacy(ctx, args, out)
    return EXIT_USAGE  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
