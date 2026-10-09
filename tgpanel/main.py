"""Service entry point: ``python -m tgpanel.main`` (tgpanel.service).

ONE asyncio process owns SQLite and the apply lock (PLAN 3.9) and runs, each in its own
supervised restart loop: the web panel (uvicorn on 127.0.0.1), the traffic collector, the
Telegram bot (long polling) and the scheduler. A crash of one component never stops the
others. SIGTERM/SIGINT stop everything gracefully: components finish, an in-flight apply is
awaited (bounded), the database is closed, the exit code is 0.

``compose`` / ``prepare`` / ``run_stack`` are separate so the end-to-end tests drive exactly
the stack that production runs (over ``FakeSystemOps``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramUnauthorizedError
from argon2 import PasswordHasher
from fastapi import FastAPI

from tgpanel.apply.config import ApplyConfig
from tgpanel.apply.pipeline import ApplyPipeline, RecoveryReport
from tgpanel.bot.app import run_bot
from tgpanel.bot.runtime import Runtime, build_runtime
from tgpanel.collector.poller import Collector
from tgpanel.collector.rollup import maybe_rollup
from tgpanel.db import repo
from tgpanel.db.connection import transaction
from tgpanel.ops_cli import SETTING_BOOTSTRAPPED
from tgpanel.services.container import AppContext, build_context
from tgpanel.services.dashboard import DashboardService
from tgpanel.services.traffic import TrafficService
from tgpanel.system.ops import SystemOps
from tgpanel.web.adapters import TrafficAdapter
from tgpanel.web.app import create_app
from tgpanel.web.deps import WebContext
from tgpanel.web.security import LoginLimiter

log = logging.getLogger("tgpanel.main")

DEFAULT_DB = "/var/lib/tgpanel/tgpanel.db"
DEFAULT_ENV_FILE = "/etc/tgpanel/tgpanel.env"
DEFAULT_LISTEN = "127.0.0.1:8090"
MIN_SECRET_KEY = 32
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BOT_TOKEN_RE = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,60}")
PATH_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
ACTOR = "system"

Factory = Callable[[asyncio.Event], Awaitable[None]]


class ConfigError(Exception):
    """Fatal configuration problem: the message is printed, the service does not start."""


# ----------------------------------------------------------------------------- environment


@dataclass(frozen=True, slots=True)
class AppEnv:
    bot_token: str
    admin_ids: tuple[int, ...]
    panel_domain: str
    panel_path: str
    secret_key: str = field(repr=False)
    db_path: str
    listen_host: str
    listen_port: int
    env_file: str

    @property
    def bot_token_valid(self) -> bool:
        return BOT_TOKEN_RE.fullmatch(self.bot_token) is not None


def parse_listen(value: str) -> tuple[str, int]:
    text = value.strip() or DEFAULT_LISTEN
    host, sep, port_text = text.rpartition(":")
    if not sep:
        host, port_text = "127.0.0.1", text
    host = host.strip("[]") or "127.0.0.1"
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            "TGPANEL_LISTEN: панель слушает только 127.0.0.1 (наружу её открывает Caddy)"
        )
    if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ConfigError("TGPANEL_LISTEN: некорректный порт")
    return host, int(port_text)


def parse_admin_ids(value: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in re.split(r"[,\s;]+", value.strip()):
        if not part:
            continue
        if not part.isascii() or not part.isdigit() or not 0 < int(part) <= 2**53:
            raise ConfigError("TGPANEL_ADMIN_IDS: ожидается список Telegram ID через запятую")
        ids.append(int(part))
    return tuple(dict.fromkeys(ids))


def load_env(environ: Mapping[str, str]) -> AppEnv:
    secret = environ.get("TGPANEL_SECRET_KEY", "")
    if len(secret) < MIN_SECRET_KEY:
        raise ConfigError(
            f"TGPANEL_SECRET_KEY отсутствует или короче {MIN_SECRET_KEY} символов: "
            "служба не запущена (выполните tgpanel repair или переустановите панель)"
        )
    path = environ.get("TGPANEL_PANEL_PATH", "").strip().strip("/")
    if not PATH_RE.fullmatch(path):
        raise ConfigError("TGPANEL_PANEL_PATH отсутствует или содержит недопустимые символы")
    host, port = parse_listen(environ.get("TGPANEL_LISTEN", ""))
    return AppEnv(
        bot_token=environ.get("TGPANEL_BOT_TOKEN", "").strip(),
        admin_ids=parse_admin_ids(environ.get("TGPANEL_ADMIN_IDS", "")),
        panel_domain=environ.get("TGPANEL_PANEL_DOMAIN", "").strip().lower(),
        panel_path=path,
        secret_key=secret,
        db_path=environ.get("TGPANEL_DB", "").strip() or DEFAULT_DB,
        listen_host=host,
        listen_port=port,
        env_file=environ.get("TGPANEL_ENV_FILE", "").strip() or DEFAULT_ENV_FILE,
    )


# --------------------------------------------------------------------------------- env file


def update_env_text(text: str, key: str, value: str) -> str:
    """``KEY=value`` replaced (or appended); every other line stays byte for byte."""
    if not ENV_KEY_RE.fullmatch(key):
        raise ValueError("bad env key")
    if not value or any(c in value for c in "\n\r\0"):
        raise ValueError("bad env value")
    lines = text.splitlines()
    out: list[str] = []
    done = False
    for line in lines:
        if not done and line.split("=", 1)[0].strip() == key and "=" in line:
            out.append(f"{key}={value}")
            done = True
        elif line.split("=", 1)[0].strip() == key and "=" in line:
            continue  # duplicate definition: keep one
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def write_env_file(path: str, key: str, value: str) -> None:
    """Atomic update of one variable in the env file (mode 0600, other lines kept)."""
    target = Path(path)
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    new_text = update_env_text(current, key, value)
    fd, tmp = tempfile.mkstemp(prefix=".tgpanel-env-", dir=str(target.parent))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def make_write_env(path: str) -> Callable[[str, str], Awaitable[None]]:
    async def write_env(key: str, value: str) -> None:
        await asyncio.to_thread(write_env_file, path, key, value)

    return write_env


# ----------------------------------------------------------------------------------- logging

_HEX_RE = re.compile(r"(?i)(?:dd)?[0-9a-f]{32}")
_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


class ScrubFilter(logging.Filter):
    """Masks proxy secrets, bot tokens and the given literal values in every log record.

    Also drops anything below INFO: the service never logs at DEBUG.
    """

    def __init__(self, literals: Iterable[str] = ()) -> None:
        super().__init__()
        self._literals = sorted({v for v in literals if len(v) >= 8}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, "[redacted]")
        return _TOKEN_RE.sub("[redacted]", _HEX_RE.sub("[redacted]", text))

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return False
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self.scrub(message)
        record.args = None
        if record.exc_info:
            record.exc_text = self.scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True


QUIET_LOGGERS = ("aiogram", "aiohttp", "aiohttp.access", "httpx", "httpcore", "uvicorn.access")


def configure_logging(secrets_to_mask: Iterable[str] = (), stream: Any = None) -> ScrubFilter:
    """Root logger INFO on one scrubbed stream handler (journald adds the timestamps)."""
    scrub_filter = ScrubFilter(secrets_to_mask)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(scrub_filter)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return scrub_filter


# ------------------------------------------------------------------------------ bot token


def read_env_file_token(path: str) -> str | None:
    """``TGPANEL_BOT_TOKEN`` from the env file, if it holds a well-formed token."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "TGPANEL_BOT_TOKEN":
            found = value.strip().strip("\"'")
    if found is not None and BOT_TOKEN_RE.fullmatch(found):
        return found
    return None


def resolve_bot_token(env: AppEnv) -> str | None:
    """The env FILE wins over the process environment (the panel rewrites the file; the
    process environment is frozen at service start). Read at every bot (re)start."""
    return read_env_file_token(env.env_file) or (env.bot_token if env.bot_token_valid else None)


class BotControl:
    """Lets the panel restart ONLY the bot task (the supervisor keeps everything else running)."""

    def __init__(self) -> None:
        self.running = False
        self._restart = asyncio.Event()

    async def restart(self) -> bool:
        """True if a running bot task will restart (and re-read the token)."""
        if not self.running:
            return False
        self._restart.set()
        return True

    @property
    def restart_event(self) -> asyncio.Event:
        return self._restart


# ---------------------------------------------------------------------------------- supervisor


class Supervisor:
    """Runs components, each in its own restart loop with exponential backoff.

    A component that raises or returns before ``stop`` is set is restarted after
    ``base .. cap`` seconds (the delay resets after a run longer than ``healthy_after``).
    Only the exception type and a scrubbed text are logged.
    """

    def __init__(
        self,
        stop: asyncio.Event,
        *,
        scrub: Callable[[str], str] = lambda s: s,
        base_delay_s: float = 1.0,
        max_delay_s: float = 60.0,
        healthy_after_s: float = 30.0,
        grace_s: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stop = stop
        self._scrub = scrub
        self._base = base_delay_s
        self._cap = max_delay_s
        self._healthy = healthy_after_s
        self._grace = grace_s
        self._mono = monotonic
        self._components: dict[str, Factory] = {}
        self.restarts: dict[str, int] = {}

    def add(self, name: str, factory: Factory) -> None:
        self._components[name] = factory
        self.restarts[name] = 0

    async def _loop(self, name: str, factory: Factory) -> None:
        delay = self._base
        while not self.stop.is_set():
            started = self._mono()
            try:
                await factory(self.stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "component %s crashed: %s: %s",
                    name,
                    type(exc).__name__,
                    self._scrub(str(exc))[:300],
                )
            else:
                if self.stop.is_set():
                    return
                log.warning("component %s exited unexpectedly", name)
            if self.stop.is_set():
                return
            if self._mono() - started >= self._healthy:
                delay = self._base
            self.restarts[name] += 1
            log.warning("component %s restarts in %.1f s", name, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), delay)
            delay = min(delay * 2, self._cap)

    async def run(self) -> None:
        """Until ``stop`` is set and every component finished (or was cancelled after grace)."""
        tasks = {
            name: asyncio.create_task(self._loop(name, factory), name=f"component-{name}")
            for name, factory in self._components.items()
        }
        await self.stop.wait()
        pending = set(tasks.values())
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self._grace)
        for task in pending:
            log.warning("component %s did not stop in time: cancelled", task.get_name())
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


# ------------------------------------------------------------------------------------ stack


@dataclass
class Stack:
    env: AppEnv
    ctx: AppContext
    runtime: Runtime
    collector: Collector
    traffic: TrafficService
    dashboard: DashboardService
    web: WebContext
    app: FastAPI
    bot_control: BotControl = field(default_factory=BotControl)

    @property
    def pipeline(self) -> ApplyPipeline:
        return self.ctx.pipeline

    @property
    def bot_enabled(self) -> bool:
        return resolve_bot_token(self.env) is not None


def compose(
    env: AppEnv,
    ops: SystemOps,
    *,
    config: ApplyConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session: BaseSession | None = None,
    password_hasher: PasswordHasher | None = None,
    limiter: LoginLimiter | None = None,
    extra_hosts: Iterable[str] = (),
    scheduler_tick_s: float = 60.0,
) -> Stack:
    """Build every part and connect them (no I/O except opening the database)."""
    ctx = build_context(ops, env.db_path, config=config, clock=clock, sleep=sleep)
    traffic = TrafficService(ctx.db, clock) if clock is not None else TrafficService(ctx.db)
    dashboard_kwargs: dict[str, Any] = {}
    if clock is not None:
        dashboard_kwargs["now"] = clock
    dashboard = DashboardService(
        traffic,
        ops,
        ctx.db,
        units=(ctx.pipeline.config.relay_unit, "caddy"),
        **dashboard_kwargs,
    )
    pipeline = ctx.pipeline
    runtime = build_runtime(
        ctx,
        traffic=traffic,
        token=env.bot_token if env.bot_token_valid else None,
        maybe_rollup=lambda now: maybe_rollup(pipeline, now),
        clock=clock,
        sleep=sleep,
        session=session,
        tick_s=scheduler_tick_s,
    )
    collector = (
        Collector(ops, pipeline, clock=clock) if clock is not None else Collector(ops, pipeline)
    )
    hosts = {h.lower() for h in extra_hosts}
    if env.panel_domain:
        hosts.add(env.panel_domain)
    control = BotControl()
    web_kwargs: dict[str, Any] = {}
    if password_hasher is not None:
        web_kwargs["password_hasher"] = password_hasher
    if limiter is not None:
        web_kwargs["limiter"] = limiter
    web = WebContext(
        app=ctx,
        traffic=TrafficAdapter(traffic, dashboard),
        requests=runtime.requests_port,
        broadcast=runtime.broadcast_port,
        secret_key=env.secret_key,
        clock=clock,
        write_env=make_write_env(env.env_file),
        restart_bot=control.restart,
        extra_hosts=frozenset(hosts),
        **web_kwargs,
    )
    app = create_app(web, "/" + env.panel_path)
    return Stack(env, ctx, runtime, collector, traffic, dashboard, web, app, control)


async def prepare(stack: Stack) -> RecoveryReport | None:
    """Service start: runtime dirs, crash recovery, host name, first-run bootstrap admins."""
    report = await stack.ctx.start(recover=True)
    if report is not None:
        for message in report.messages:
            log.warning("startup recovery: %s", message)
    env = stack.env
    pipeline = stack.pipeline

    def bootstrap(conn: Any) -> int:
        added = 0
        with transaction(conn):
            if env.panel_domain and not repo.get_setting(conn, "panel_hostname", ""):
                repo.set_setting(conn, "panel_hostname", env.panel_domain)
            # same semantics as `ops_cli bootstrap`: admins only on the FIRST start, so an
            # administrator removed in the panel is never silently added back
            if not repo.get_setting(conn, SETTING_BOOTSTRAPPED):
                now = pipeline.now()
                for tg_id in env.admin_ids:
                    repo.add_admin(conn, tg_id, now)
                    added += 1
                repo.set_setting(conn, SETTING_BOOTSTRAPPED, "1")
                repo.add_audit(conn, now, ACTOR, "install.bootstrap", "", "")
        return added

    await pipeline.db_write(bootstrap)
    return report


# ----------------------------------------------------------------------------- components


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers (the service owns SIGTERM/SIGINT)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def install_signal_handlers(self) -> None:  # older uvicorn versions
        return None


class WebStartupError(RuntimeError):
    pass


def uvicorn_config(app: FastAPI, host: str, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
        h11_max_incomplete_event_size=16 * 1024,
        http="h11",
        ws="none",
        lifespan="off",
        log_config=None,
        log_level="warning",
    )


def web_component(stack: Stack, host: str | None = None, port: int | None = None) -> Factory:
    async def run(stop: asyncio.Event) -> None:
        config = uvicorn_config(
            stack.app, host or stack.env.listen_host, port or stack.env.listen_port
        )
        server = _Server(config)

        async def serve() -> None:
            try:
                await server.serve()
            except SystemExit:  # uvicorn exits the process on a bind error
                raise WebStartupError("веб-сервер не запустился (порт занят?)") from None

        serving = asyncio.create_task(serve())
        stopper = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({serving, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if not serving.done():
                server.should_exit = True
                await serving
            else:
                serving.result()
        finally:
            stopper.cancel()
            if not serving.done():
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)

    return run


def collector_component(stack: Stack) -> Factory:
    return stack.collector.run


def bot_component(stack: Stack) -> Factory:
    async def run(stop: asyncio.Event) -> None:
        rt = stack.runtime
        control = stack.bot_control
        while not stop.is_set():
            token = await asyncio.to_thread(resolve_bot_token, stack.env)
            if token is None:
                log.error("токен бота не задан: бот ждёт токен (задайте его в настройках панели)")
                await _wait_any(stop, control.restart_event)
                control.restart_event.clear()
                continue
            child = asyncio.Event()
            control.restart_event.clear()
            control.running = True
            forward = asyncio.create_task(_forward(stop, control.restart_event, child))
            try:
                await run_bot(token, rt.deps, rt.sender, child, session=rt.session)
            except TelegramUnauthorizedError:
                log.critical(
                    "Telegram отклонил токен бота: бот отключён, остальные части панели "
                    "работают. Задайте верный токен в настройках панели."
                )
                await _wait_any(stop, control.restart_event)
            finally:
                control.running = False
                forward.cancel()
                await asyncio.gather(forward, return_exceptions=True)
            if control.restart_event.is_set() and not stop.is_set():
                log.info("бот перезапускается (новый токен)")
                control.restart_event.clear()

    return run


async def _wait_any(*events: asyncio.Event) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _forward(stop: asyncio.Event, restart: asyncio.Event, child: asyncio.Event) -> None:
    """Stop the running bot when the service stops or a restart is requested."""
    await _wait_any(stop, restart)
    child.set()


def scheduler_component(stack: Stack) -> Factory:
    async def run(stop: asyncio.Event) -> None:
        rt = stack.runtime
        if stack.bot_enabled:
            # notices and reminders need the bot: wait (bounded) until it is bound, but never
            # keep a shutdown waiting for it
            ready = asyncio.create_task(rt.sender.wait_ready(rt.ready_timeout_s))
            stopper = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({ready, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
                stopper.cancel()
                await asyncio.gather(ready, stopper, return_exceptions=True)
            if stop.is_set():
                return
        await rt.scheduler.run(stop)

    return run


def build_components(
    stack: Stack, *, host: str | None = None, port: int | None = None, serve_web: bool = True
) -> dict[str, Factory]:
    components: dict[str, Factory] = {}
    if serve_web:
        components["web"] = web_component(stack, host, port)
    components["collector"] = collector_component(stack)
    if stack.bot_enabled:
        components["bot"] = bot_component(stack)
    else:
        log.error(
            "ВНИМАНИЕ: токен бота не задан или неверного формата (TGPANEL_BOT_TOKEN): "
            "Telegram-бот отключён, панель продолжает работать"
        )
    components["scheduler"] = scheduler_component(stack)
    return components


async def wait_apply_idle(pipeline: ApplyPipeline, timeout_s: float, poll_s: float = 0.1) -> bool:
    """Wait (bounded) until no apply is running or queued. True when idle."""
    deadline = time.monotonic() + timeout_s
    while pipeline.is_applying:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)
    return True


async def run_stack(
    stack: Stack,
    stop: asyncio.Event,
    *,
    components: Mapping[str, Factory] | None = None,
    overrides: Mapping[str, Factory] | None = None,
    supervisor: Supervisor | None = None,
    apply_wait_s: float = 60.0,
    close: bool = True,
) -> int:
    """Run every component until ``stop``; then wait for an in-flight apply and close the DB."""
    parts = dict(components if components is not None else build_components(stack))
    parts.update(overrides or {})
    sup = supervisor or Supervisor(stop)
    for name, factory in parts.items():
        sup.add(name, factory)
    try:
        await sup.run()
    finally:
        if not await wait_apply_idle(stack.pipeline, apply_wait_s):
            log.warning(
                "применение не завершилось за %.0f с; при следующем запуске выполнится "
                "восстановление",
                apply_wait_s,
            )
        if close:
            stack.ctx.close()
    log.info("tgpanel stopped")
    return 0


# ------------------------------------------------------------------------------------- main


async def _amain(env: AppEnv, ops: SystemOps) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stack = compose(env, ops)
    try:
        await prepare(stack)
    except BaseException:
        stack.ctx.close()
        raise
    log.info("tgpanel started (panel on %s:%d)", env.listen_host, env.listen_port)
    scrub_filter = ScrubFilter([env.secret_key, env.bot_token])
    sup = Supervisor(stop, scrub=scrub_filter.scrub)
    return await run_stack(stack, stop, supervisor=sup)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    env_map = os.environ if environ is None else environ
    try:
        env = load_env(env_map)
    except ConfigError as exc:
        configure_logging()
        log.critical("%s", exc)
        return 2
    configure_logging([env.secret_key, env.bot_token])
    from tgpanel.system.real import RealSystemOps

    try:
        return asyncio.run(_amain(env, RealSystemOps()))
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except Exception as exc:
        scrubbed = ScrubFilter([env.secret_key, env.bot_token]).scrub(str(exc))[:300]
        log.critical("tgpanel failed to start: %s: %s", type(exc).__name__, scrubbed)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
