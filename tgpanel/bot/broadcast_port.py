"""Adapters that give the web panel its ``BroadcastPort`` and ``RequestsPort``.

The web layer never imports the bot; the orchestrator hands it these objects. Previews contain
no link or secret: the link placeholders are replaced by ``[ссылка]``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.bot import texts
from tgpanel.db import repo
from tgpanel.db.connection import Database, transaction
from tgpanel.domain.expiry import Term
from tgpanel.services.api import UserService
from tgpanel.services.broadcast import BroadcastService, check_template
from tgpanel.services.notifier import (
    FORBIDDEN,
    SENT,
    LinkButton,
    Messenger,
    Templates,
    message_values,
    render_message,
    scrub_secrets,
)
from tgpanel.services.requests import RequestKind, RequestService
from tgpanel.web.deps import (
    BroadcastItem,
    BroadcastPreview,
    BroadcastReport,
    DecisionResult,
    LinkSendResult,
)

log = logging.getLogger("tgpanel.bot")
LINK_PLACEHOLDER = "[ссылка]"


class BroadcastPortAdapter:
    def __init__(
        self,
        service: BroadcastService,
        users: UserService,
        db: Database,
        pipeline: ApplyPipeline,
        messenger: Messenger,
    ) -> None:
        self._service = service
        self._users = users
        self._db = db
        self._pipeline = pipeline
        self._messenger = messenger
        self._templates = Templates(db)
        self.tasks: set[asyncio.Task[Any]] = set()

    async def wait_background(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # ------------------------------------------------------------------ BroadcastPort

    async def preview(self, template: str, user_ids: Sequence[int]) -> BroadcastPreview:
        try:
            text = check_template(template)
        except OperationRejected as exc:
            return BroadcastPreview(0, 0, 0, "", error=str(exc))
        pv = await self._service.preview(list(user_ids))
        sample = ""
        if pv.included:
            first = await self._users.get(pv.included[0].user_id)
            if first is not None:
                tz = (await self._db.run(repo.get_setting, "timezone", "UTC")) or "UTC"
                values = message_values(
                    first, LINK_PLACEHOLDER, LINK_PLACEHOLDER, tz, self._pipeline.now()
                )
                sample = render_message(text, values, escape=False)
        return BroadcastPreview(
            recipients=len(pv.recipients),
            sendable=len(pv.included),
            skipped=len(pv.excluded),
            sample=sample,
        )

    async def start(self, template: str, user_ids: Sequence[int], actor: str) -> int:
        bid = await self._service.create(template, actor, list(user_ids))
        task = asyncio.create_task(self._service.run(bid))
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return bid

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.warning(
                "broadcast task failed: %s: %s", type(exc).__name__, scrub_secrets(str(exc), 200)
            )

    async def report(self, broadcast_id: int) -> BroadcastReport | None:
        try:
            rep = await self._service.report(broadcast_id)
        except OperationRejected:
            return None
        running = self._service.is_running(broadcast_id) or rep.pending > 0
        items = []
        for it in rep.items:
            result, note = _item_status(it.result, it.result_text)
            items.append(BroadcastItem(it.user_id, it.tg_id, result, note))
        return BroadcastReport(
            broadcast_id=broadcast_id,
            status="running" if running else "done",
            total=rep.total,
            sent=rep.sent,
            failed=rep.errors + rep.forbidden,
            skipped=rep.skipped,
            items=tuple(items),
        )

    async def send_links(self, user_ids: Sequence[int], actor: str) -> list[LinkSendResult]:
        tz = (await self._db.run(repo.get_setting, "timezone", "UTC")) or "UTC"
        out: list[LinkSendResult] = []
        for uid in dict.fromkeys(user_ids):
            out.append(await self._send_link(uid, tz))
        sent = sum(1 for r in out if r.ok)

        def audit(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                repo.add_audit(
                    conn,
                    self._pipeline.now(),
                    actor,
                    "user.send_links",
                    f"users:{len(out)}",
                    f"sent={sent}",
                )

        try:
            await self._pipeline.db_write(audit)
        except Exception as exc:
            log.warning("send_links audit failed: %s", type(exc).__name__)
        return out

    async def _send_link(self, uid: int, tz: str) -> LinkSendResult:
        user = await self._users.get(uid)
        extra = await self._db.run(repo.get_user_extra, uid)
        if user is None or extra is None:
            return LinkSendResult(uid, False, "Пользователь не найден")
        if user.tg_id is None:
            return LinkSendResult(uid, False, _cap(texts.REASON_NO_TG))
        if user.status.value != "active":
            return LinkSendResult(uid, False, "Профиль отключён или срок истёк")
        if not extra.bot_started:
            return LinkSendResult(uid, False, _cap(texts.REASON_NOT_STARTED))
        if not extra.can_message:
            return LinkSendResult(uid, False, _cap(texts.REASON_BLOCKED))
        try:
            link, tg_link = self._users.link(user), self._users.tg_link(user)
        except Exception:
            return LinkSendResult(uid, False, "Не задано имя хоста прокси")
        default = texts.DEFAULT_LINK.replace("{intro}", texts.LINK_FROM_ADMIN)
        text = await self._templates.render(
            "msg.link", default, message_values(user, link, tg_link, tz, self._pipeline.now())
        )
        result = await self._messenger.deliver(
            user.tg_id, text, LinkButton(texts.BTN_CONNECT, link)
        )
        if result == SENT:
            return LinkSendResult(uid, True)
        if result == FORBIDDEN:
            return LinkSendResult(uid, False, _cap(texts.REASON_BLOCKED))
        return LinkSendResult(uid, False, "Не удалось отправить сообщение")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _item_status(result: str, text: str) -> tuple[str, str]:
    if result == "pending":
        return "pending", ""
    if result == SENT:
        return "sent", ""
    if result == FORBIDDEN:
        return "blocked", text
    if result.startswith("skipped:"):
        return "skipped", text
    return "failed", text


class RequestsPortAdapter:
    """``RequestsPort`` over the SAME ``RequestService`` instance the bot uses."""

    def __init__(
        self, requests: RequestService, users: UserService, db: Database, messenger: Messenger
    ) -> None:
        self._requests = requests
        self._users = users
        self._db = db
        self._messenger = messenger
        self._templates = Templates(db)

    async def approve(self, request_id: int, term: Term | None, actor: str) -> DecisionResult:
        out = await self._requests.approve(request_id, term, actor)
        if out.kind is RequestKind.ISSUED and out.request is not None and out.user is not None:
            link = out.link or ""
            try:
                tg_link = self._users.tg_link(out.user)
            except Exception:
                tg_link = ""
            tz = (await self._db.run(repo.get_setting, "timezone", "UTC")) or "UTC"
            text = await self._templates.render(
                "msg.approved",
                texts.DEFAULT_APPROVED,
                message_values(out.user, link, tg_link, tz, out.request.created_at),
            )
            button = LinkButton(texts.BTN_CONNECT, link) if link else None
            await self._messenger.deliver(out.request.tg_id, text, button)
            return DecisionResult(True)
        return DecisionResult(False, _error_text(out.kind, out.error))

    async def reject(self, request_id: int, actor: str) -> DecisionResult:
        out = await self._requests.reject(request_id, actor)
        if out.kind is RequestKind.REJECTED and out.request is not None:
            text = await self._templates.render(
                "msg.rejected", texts.DEFAULT_REJECTED, {"name": out.request.full_name}
            )
            await self._messenger.deliver(out.request.tg_id, text)
            return DecisionResult(True)
        return DecisionResult(False, _error_text(out.kind, out.error))


def _error_text(kind: RequestKind, error: str | None) -> str:
    if kind is RequestKind.ALREADY_DECIDED:
        return "Заявка уже обработана"
    if kind is RequestKind.NOT_FOUND:
        return "Заявка не найдена"
    return scrub_secrets(error or "Не удалось выполнить действие", 300)
