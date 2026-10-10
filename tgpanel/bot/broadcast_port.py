"""Adapters that give the web panel its ``BroadcastPort`` and ``RequestsPort``.

The web layer never imports the bot; the orchestrator hands it these objects. Previews contain
no link or secret: the link placeholders are replaced by ``[ссылка]``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from tgpanel.apply.errors import OperationRejected
from tgpanel.apply.pipeline import ApplyPipeline
from tgpanel.bot import texts
from tgpanel.db import repo
from tgpanel.db.connection import Database
from tgpanel.domain.expiry import Term
from tgpanel.services.api import UserService
from tgpanel.services.broadcast import BroadcastService, LinkDelivery, check_template
from tgpanel.services.notifier import (
    FORBIDDEN,
    SENT,
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
        link_delivery: LinkDelivery,
    ) -> None:
        self._delivery = link_delivery
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
        results = await self._delivery.deliver(
            list(user_ids), actor, default=texts.DEFAULT_LINK_PLAIN
        )
        return [LinkSendResult(r.user_id, r.ok, _cap(r.note)) for r in results]


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
        self,
        requests: RequestService,
        users: UserService,
        db: Database,
        messenger: Messenger,
        link_delivery: LinkDelivery,
    ) -> None:
        self._requests = requests
        self._users = users
        self._db = db
        self._messenger = messenger
        self._delivery = link_delivery
        self._templates = Templates(db)

    async def approve(self, request_id: int, term: Term | None, actor: str) -> DecisionResult:
        out = await self._requests.approve(request_id, term, actor)
        if out.kind is RequestKind.ISSUED and out.request is not None and out.user is not None:
            (sent,) = await self._delivery.deliver(
                [out.user.id], actor, key="msg.approved", default=texts.DEFAULT_APPROVED
            )
            if sent.ok:
                return DecisionResult(True)
            # the profile exists and the request is closed: report the delivery problem
            return DecisionResult(True, error=_cap(sent.note) or "Ссылка не доставлена")
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
