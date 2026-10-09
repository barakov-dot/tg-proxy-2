"""Unit tests of the service entry point: environment, env file, log scrubbing, supervisor."""

from __future__ import annotations

import asyncio
import io
import logging
import stat
from pathlib import Path

import pytest

from tests.apply.conftest import Clock, no_sleep
from tgpanel import main as app_main
from tgpanel.apply.config import ApplyConfig
from tgpanel.db import repo
from tgpanel.main import (
    AppEnv,
    ConfigError,
    ScrubFilter,
    Supervisor,
    compose,
    configure_logging,
    load_env,
    parse_admin_ids,
    parse_listen,
    prepare,
    update_env_text,
    write_env_file,
)
from tgpanel.system.fake import FakeSystemOps

GOOD = {
    "TGPANEL_SECRET_KEY": "s" * 40,
    "TGPANEL_PANEL_PATH": "abc-DEF_123",
    "TGPANEL_PANEL_DOMAIN": "Panel.Example.com",
    "TGPANEL_ADMIN_IDS": "42, 43",
    "TGPANEL_BOT_TOKEN": "123456:" + "B" * 35,
}


def test_load_env_defaults_and_values() -> None:
    env = load_env(GOOD)
    assert env.panel_path == "abc-DEF_123" and env.panel_domain == "panel.example.com"
    assert env.admin_ids == (42, 43) and env.bot_token_valid
    assert (env.listen_host, env.listen_port) == ("127.0.0.1", 8090)
    assert env.db_path == "/var/lib/tgpanel/tgpanel.db"
    assert env.env_file == "/etc/tgpanel/tgpanel.env"
    assert "s" * 40 not in repr(env)  # the key never shows up in reprs/logs


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"TGPANEL_SECRET_KEY": "short"}, "TGPANEL_SECRET_KEY"),
        ({"TGPANEL_SECRET_KEY": ""}, "TGPANEL_SECRET_KEY"),
        ({"TGPANEL_PANEL_PATH": ""}, "TGPANEL_PANEL_PATH"),
        ({"TGPANEL_PANEL_PATH": "a/../b"}, "TGPANEL_PANEL_PATH"),
        ({"TGPANEL_LISTEN": "0.0.0.0:8090"}, "127.0.0.1"),
        ({"TGPANEL_LISTEN": "127.0.0.1:99999"}, "порт"),
        ({"TGPANEL_ADMIN_IDS": "abc"}, "TGPANEL_ADMIN_IDS"),
    ],
)
def test_load_env_refuses_bad_configuration(patch: dict[str, str], needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        load_env({**GOOD, **patch})


def test_main_refuses_to_start_with_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert app_main.main(environ={**GOOD, "TGPANEL_SECRET_KEY": "x" * 10}) == 2
    assert "TGPANEL_SECRET_KEY" in capsys.readouterr().err
    logging.getLogger().handlers.clear()


def test_listen_and_admin_parsing() -> None:
    assert parse_listen("8091") == ("127.0.0.1", 8091)
    assert parse_listen("localhost:9000") == ("localhost", 9000)
    assert parse_listen("") == ("127.0.0.1", 8090)
    assert parse_admin_ids("1;2 3,3") == (1, 2, 3)
    assert parse_admin_ids("") == ()


def test_update_env_text_keeps_other_lines() -> None:
    text = "# comment\nA=1\nTGPANEL_BOT_TOKEN=old\n\nB=2\n"
    out = update_env_text(text, "TGPANEL_BOT_TOKEN", "new:token")
    assert out == "# comment\nA=1\nTGPANEL_BOT_TOKEN=new:token\n\nB=2\n"
    assert update_env_text("", "K", "v") == "K=v\n"
    assert update_env_text("A=1\nK=1\nK=2\n", "K", "v") == "A=1\nK=v\n"
    for bad_key, bad_value in (("lower", "v"), ("K", "a\nB=1"), ("K", "")):
        with pytest.raises(ValueError, match="bad env"):
            update_env_text("", bad_key, bad_value)


def test_write_env_file_is_atomic_and_private(tmp_path: Path) -> None:
    target = tmp_path / "tgpanel.env"
    target.write_text("TGPANEL_SECRET_KEY=keep\nTGPANEL_BOT_TOKEN=old\n")
    target.chmod(0o644)
    write_env_file(str(target), "TGPANEL_BOT_TOKEN", "123456:" + "C" * 35)
    assert target.read_text() == f"TGPANEL_SECRET_KEY=keep\nTGPANEL_BOT_TOKEN=123456:{'C' * 35}\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["tgpanel.env"]  # no temp leftovers
    fresh = tmp_path / "new.env"
    write_env_file(str(fresh), "K", "v")
    assert fresh.read_text() == "K=v\n" and stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_scrub_filter_masks_secrets_tokens_and_literals_and_drops_debug() -> None:
    stream = io.StringIO()
    configure_logging(["my-signing-key-value"], stream)
    log = logging.getLogger("tgpanel.test")
    secret = "ab" * 16
    log.info("a %s b dd%s c 123456789:%s d my-signing-key-value", secret, secret, "X" * 35)
    log.debug("debug line must not appear")
    try:
        raise RuntimeError(f"boom {secret}")
    except RuntimeError:
        log.exception("failed")
    logging.getLogger("aiogram.dispatcher").info("noisy")
    logging.getLogger("uvicorn.access").info("GET /secret-path")
    out = stream.getvalue()
    assert secret not in out and "123456789:" not in out and "my-signing-key-value" not in out
    assert out.count("[redacted]") >= 5 and "debug line" not in out
    assert "noisy" not in out and "secret-path" not in out
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("aiogram").level == logging.WARNING
    logging.getLogger().handlers.clear()


def test_scrub_filter_survives_bad_format_args() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "%s %s", ("only-one",), None)
    assert ScrubFilter().filter(record)
    assert "%s" in record.getMessage()


async def test_supervisor_isolates_and_restarts_with_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    stop = asyncio.Event()
    calls = {"bad": 0, "good": 0}

    async def bad(_: asyncio.Event) -> None:
        calls["bad"] += 1
        raise RuntimeError("password=" + "ab" * 16)

    async def good(ev: asyncio.Event) -> None:
        calls["good"] += 1
        await ev.wait()

    sup = Supervisor(
        stop,
        scrub=ScrubFilter().scrub,
        base_delay_s=0.01,
        max_delay_s=0.04,
        monotonic=lambda: 0.0,
    )
    sup.add("bad", bad)
    sup.add("good", good)
    runner = asyncio.create_task(sup.run())
    async with asyncio.timeout(5):
        while calls["bad"] < 4:  # noqa: ASYNC110 - polling a counter of a supervised crash loop
            await asyncio.sleep(0.01)
    assert calls["good"] == 1 and not runner.done()
    stop.set()
    await asyncio.wait_for(runner, 5)
    assert sup.restarts["bad"] >= 3 and sup.restarts["good"] == 0
    assert "ab" * 16 not in caplog.text and "component bad crashed: RuntimeError" in caplog.text


async def test_supervisor_cancels_a_component_that_ignores_stop() -> None:
    stop = asyncio.Event()

    async def stubborn(_: asyncio.Event) -> None:
        await asyncio.sleep(3600)

    sup = Supervisor(stop, grace_s=0.05)
    sup.add("stubborn", stubborn)
    runner = asyncio.create_task(sup.run())
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(runner, 5)


async def test_prepare_adds_bootstrap_admins_only_on_the_first_start(tmp_path: Path) -> None:
    fake = FakeSystemOps()
    fake.seed_upstream("clean")
    env = AppEnv(
        "",
        (7, 8),
        "panel.example.com",
        "p",
        "k" * 40,
        str(tmp_path / "t.db"),
        "127.0.0.1",
        8090,
        str(tmp_path / "e"),
    )
    stack = compose(env, fake, config=ApplyConfig(), clock=Clock(), sleep=no_sleep)
    try:
        await prepare(stack)
        assert stack.ctx.db.call(repo.list_admins) == [7, 8]
        assert stack.ctx.db.call(repo.get_setting, "panel_hostname") == "panel.example.com"
        stack.ctx.db.call(repo.remove_admin, 8)
        await prepare(stack)  # restart: the removed administrator is NOT added back
        assert stack.ctx.db.call(repo.list_admins) == [7]
        assert not stack.bot_enabled
    finally:
        stack.ctx.close()
