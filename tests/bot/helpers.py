"""Test doubles for the bot: mocked Telegram session, fake sender, fake traffic, feeding updates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)

from tgpanel.services.notifier import LinkButton

TOKEN = "123456:TESTTOKEN"


class MockSession(BaseSession):
    """Records every API call; never touches the network."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []
        self.errors: dict[tuple[str, int], list[Exception]] = {}
        self._mid = 1000
        self.answered: set[str] = set()
        # Telegram rejects a second answerCallbackQuery for the same query (and any answer
        # that comes too late); the mock behaves the same so such bugs show up in tests.
        self.strict_answers = True
        self.fail_answers = False  # every answerCallbackQuery is rejected (query too old)

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: Any = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        chat_id = getattr(method, "chat_id", 0)
        queue = self.errors.get((name, int(chat_id) if isinstance(chat_id, int) else 0))
        if queue:
            raise queue.pop(0)
        if name == "AnswerCallbackQuery" and self.fail_answers:
            raise TelegramBadRequest(
                method=method, message="Bad Request: query is too old and response timeout expired"
            )
        if name == "AnswerCallbackQuery" and self.strict_answers:
            qid = str(method.callback_query_id)  # type: ignore[attr-defined]
            if qid in self.answered:
                raise TelegramBadRequest(
                    method=method,
                    message="Bad Request: query is too old and response timeout expired",
                )
            self.answered.add(qid)
        self.calls.append((name, method))
        if name == "GetUpdates":
            await asyncio.sleep(0.01)
            return []
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="bot", username="testbot")
        if name in ("SendMessage", "EditMessageText", "SendPhoto"):
            self._mid += 1
            return Message(
                message_id=self._mid,
                date=datetime.now(UTC),
                chat=Chat(id=int(chat_id) if isinstance(chat_id, int) else 0, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    # ------------------------------------------------------------------ inspection

    def sent(self, chat_id: int, name: str = "SendMessage") -> list[Any]:
        return [m for n, m in self.calls if n == name and getattr(m, "chat_id", None) == chat_id]

    def texts(self, chat_id: int) -> list[str]:
        return [
            str(m.text)
            for n, m in self.calls
            if n in ("SendMessage", "EditMessageText") and m.chat_id == chat_id
        ]

    def photos(self, chat_id: int) -> list[Any]:
        return self.sent(chat_id, "SendPhoto")

    def clear(self) -> None:
        self.calls.clear()

    def button_data(self, chat_id: int) -> list[str]:
        out: list[str] = []
        for n, m in self.calls:
            if n in ("SendMessage", "EditMessageText") and m.chat_id == chat_id:
                markup = m.reply_markup
                if isinstance(markup, InlineKeyboardMarkup):
                    out += [
                        b.callback_data
                        for r in markup.inline_keyboard
                        for b in r
                        if b.callback_data
                    ]
        return out

    def last_markup(self, chat_id: int) -> InlineKeyboardMarkup | None:
        found = None
        for n, m in self.calls:
            if n in ("SendMessage", "EditMessageText") and m.chat_id == chat_id:
                found = m.reply_markup
        return found if isinstance(found, InlineKeyboardMarkup) else None

    def alerts(self) -> list[str]:
        return [str(m.text) for n, m in self.calls if n == "AnswerCallbackQuery" and m.text]


def forbidden(chat_id: int = 1) -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendMessage(chat_id=chat_id, text="x"), message="Forbidden: bot was blocked"
    )


def retry_after(seconds: int, chat_id: int = 1) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=chat_id, text="x"),
        message="Too Many Requests",
        retry_after=seconds,
    )


@dataclass
class Sent:
    chat_id: int
    text: str
    button: LinkButton | None
    html: bool
    at: float


@dataclass
class FakeTime:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeSender:
    """``MessageSender`` double: records, can raise scripted errors per chat."""

    time: FakeTime = field(default_factory=FakeTime)
    sent: list[Sent] = field(default_factory=list)
    script: dict[int, list[BaseException]] = field(default_factory=dict)

    async def send_message(
        self, chat_id: int, text: str, *, button: LinkButton | None = None, html: bool = True
    ) -> None:
        queue = self.script.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append(Sent(chat_id, text, button, html, self.time.now))

    def to(self, chat_id: int) -> list[Sent]:
        return [s for s in self.sent if s.chat_id == chat_id]


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify_admins(self, text: str) -> None:
        self.messages.append(text)


class FakeTraffic:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def user_totals(self, user_id: int, period: str) -> tuple[int, int]:
        self.calls.append((user_id, period))
        return {"24h": (1024, 2048), "7d": (5 * 1024**2, 0), "30d": (0, 3 * 1024**3)}[period]


class Tg:
    """Feeds updates into a dispatcher through a mocked bot."""

    def __init__(self, dp: Dispatcher, bot: Bot, session: MockSession) -> None:
        self.dp, self.bot, self.session = dp, bot, session
        self._uid = 0

    def _user(self, uid: int, name: str = "Tester", username: str | None = None) -> User:
        return User(id=uid, is_bot=False, first_name=name, username=username)

    def _msg(self, uid: int, text: str, chat_type: str = "private", **kw: Any) -> Message:
        self._uid += 1
        entities = None
        if text.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        return Message(
            message_id=self._uid,
            date=datetime.now(UTC),
            chat=Chat(id=uid, type=chat_type),
            from_user=self._user(uid, **kw),
            text=text,
            entities=entities,
        )

    async def send(self, uid: int, text: str, **kw: Any) -> None:
        self._uid += 1
        await self.dp.feed_update(
            self.bot, Update(update_id=self._uid, message=self._msg(uid, text, **kw))
        )

    async def press(
        self,
        uid: int,
        data: str,
        chat_type: str = "private",
        *,
        inaccessible: bool = False,
        name: str = "Tester",
        username: str | None = None,
    ) -> None:
        self._uid += 1
        host: Message | InaccessibleMessage = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=uid, type=chat_type),
            from_user=User(id=999, is_bot=True, first_name="bot"),
            text="menu",
        )
        if inaccessible:
            host = InaccessibleMessage(chat=Chat(id=uid, type=chat_type), message_id=1, date=0)
        cb = CallbackQuery(
            id=str(self._uid),
            from_user=self._user(uid, name, username),
            chat_instance="ci",
            message=host,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, callback_query=cb))
