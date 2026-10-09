"""The bot task restarts alone and reads the env FILE token at every (re)start."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.apply.conftest import Clock, no_sleep
from tgpanel import main as app_main
from tgpanel.apply.config import ApplyConfig
from tgpanel.main import (
    AppEnv,
    BotControl,
    bot_component,
    compose,
    read_env_file_token,
    resolve_bot_token,
)
from tgpanel.system.fake import FakeSystemOps

OLD = "111111:" + "A" * 35
NEW = "222222:" + "B" * 35


def write_token(path: str, token: str) -> None:
    Path(path).write_text(f"TGPANEL_BOT_TOKEN={token}\n")


def make_env(tmp_path: Path, process_token: str) -> AppEnv:
    return AppEnv(
        process_token,
        (7,),
        "panel.example.com",
        "p",
        "k" * 40,
        str(tmp_path / "t.db"),
        "127.0.0.1",
        8090,
        str(tmp_path / "tgpanel.env"),
    )


def test_env_file_token_wins_over_process_environment(tmp_path: Path) -> None:
    env = make_env(tmp_path, OLD)
    assert resolve_bot_token(env) == OLD  # no file: the process value
    Path(env.env_file).write_text(f"X=1\nTGPANEL_BOT_TOKEN={NEW}\n")
    assert read_env_file_token(env.env_file) == NEW
    assert resolve_bot_token(env) == NEW  # the file wins (the process env is frozen at start)
    Path(env.env_file).write_text("TGPANEL_BOT_TOKEN=garbage\n")
    assert resolve_bot_token(env) == OLD  # a malformed file value never replaces a good one
    empty = make_env(tmp_path, "")
    Path(empty.env_file).write_text(f'TGPANEL_BOT_TOKEN="{NEW}"\n')
    assert resolve_bot_token(empty) == NEW


async def test_restart_is_refused_while_the_bot_is_not_running() -> None:
    control = BotControl()
    assert await control.restart() is False
    control.running = True
    assert await control.restart() is True and control.restart_event.is_set()


async def test_only_the_bot_task_restarts_with_the_new_token(
    tmp_path: Path, monkeypatch: Any
) -> None:
    env = make_env(tmp_path, OLD)
    write_token(env.env_file, OLD)
    fake = FakeSystemOps()
    fake.seed_upstream("clean")
    stack = compose(env, fake, config=ApplyConfig(), clock=Clock(), sleep=no_sleep)
    started: list[str] = []
    stopped: list[str] = []

    async def fake_run_bot(
        token: str, deps: Any, sender: Any, stop: asyncio.Event, **kw: Any
    ) -> None:
        started.append(token)
        await stop.wait()
        stopped.append(token)

    monkeypatch.setattr(app_main, "run_bot", fake_run_bot)
    service_stop = asyncio.Event()
    task: asyncio.Task[None] = asyncio.ensure_future(bot_component(stack)(service_stop))
    try:
        for _ in range(100):
            if stack.bot_control.running:
                break
            await asyncio.sleep(0.01)
        assert started == [OLD]
        # the panel writes the new token and asks for a restart of the bot only
        write_token(env.env_file, NEW)
        assert await stack.web.admin._restart_bot() is True  # type: ignore[misc]
        for _ in range(200):
            if started == [OLD, NEW]:
                break
            await asyncio.sleep(0.01)
        assert started == [OLD, NEW] and stopped == [OLD]
        assert not service_stop.is_set() and not task.done()  # the service keeps running
    finally:
        service_stop.set()
        await asyncio.wait_for(task, 5)
        stack.ctx.close()
    assert stopped == [OLD, NEW]
