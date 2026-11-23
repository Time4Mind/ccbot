"""Recover definitely unpasted Codex requests as soon as their editor returns."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


class ComposerBusy(Exception):
    """No part of this request has been pasted."""


def earlier_waiter(recovery: InputRecovery, request: InputRequest) -> bool:
    return any(
        r.session_id == request.session_id
        and r.sequence < request.sequence
        and r.state == "waiting_composer"
        for r in recovery.records.values()
    )


def defer(recovery: InputRecovery, request: InputRequest, prepared: str) -> None:
    request.prepared = prepared
    request.state = "waiting_composer"
    request.dispatch_started_at = 0
    request.checkpoint = None
    recovery.changed(request)


def enqueue_saved(request: InputRequest, application: Any) -> asyncio.Future[bool]:
    from telegram import Update
    from telegram.ext import CallbackContext
    from .bot import inbound
    from .inbound_queue import enqueue_inbound

    update = Update.de_json(request.update, application.bot)
    kind = (
        "command"
        if request.kind == "text"
        and update.message
        and (update.message.text or "").startswith("/")
        else request.kind
    )
    return enqueue_inbound(
        request.user_id,
        request.window_id,
        update,
        CallbackContext.from_update(update, application),
        kind=request.kind,
        processor=getattr(inbound, "_run_" + kind),
        recovery_id=request.id,
    ).completion


def restore_inputs(recovery: InputRecovery, application: Any) -> None:
    background = {
        "unknown",
        "failed",
        "awaiting_confirmation",
        "waiting_session",
        "waiting_composer",
    }
    for request in sorted(recovery.records.values(), key=lambda r: r.sequence):
        if request.state not in background:
            enqueue_saved(request, application)


async def resume_waiting(
    recovery: InputRecovery, application: Any, scheduled: set[str]
) -> None:
    from .codex_input import composer
    from .tmux_manager import tmux_manager

    checked = set()
    waiting = sorted(
        (r for r in recovery.records.values() if r.state == "waiting_composer"),
        key=lambda r: r.sequence,
    )
    for request in waiting:
        if recovery.source(request) is None:
            recovery.fail(request, "Исходная сессия недоступна. Запрос не отправлен.")
            continue
        if request.notice_id is None and recovery.clock() >= request.received_at + 60:
            request.notice = "Codex ожидает ввода в диалоге. Запрос сохранён в очереди и будет отправлен, когда освободится поле сообщения."
            await recovery.deliver_notice(request)
        if request.window_id in checked:
            continue
        checked.add(request.window_id)
        if request.id in scheduled:
            continue
        pane = await tmux_manager.capture_pane(request.window_id)
        if pane and composer(pane) is not None and request.state == "waiting_composer":
            done = enqueue_saved(request, application)
            scheduled.add(request.id)
            done.add_done_callback(lambda _, key=request.id: scheduled.discard(key))
