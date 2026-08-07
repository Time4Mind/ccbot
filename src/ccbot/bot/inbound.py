"""Fast Telegram intake handlers backed by the per-session FIFO.

Each callback pins the active tmux window before its first await, enqueues the
update, and returns. The real handlers run in a background lane, so a slow
voice transcription never blocks session-switch callbacks.
"""

from __future__ import annotations

import asyncio
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..codex_auth import get_flow
from ..handlers.message_sender import safe_reply
from ..handlers.notifications import lookup_session_for_message
from ..i18n import t
from ..inbound_queue import InboundProcessor, enqueue_inbound
from ..session import session_manager
from ._common import active_window, is_user_allowed
from .messages import (
    document_handler,
    forward_command_handler,
    photo_handler,
    text_handler,
    unsupported_content_handler,
    voice_handler,
)


async def _run_text(update: Update, context: Any, wid: str) -> bool:
    return await text_handler(update, context, pinned_wid=wid)


async def _run_command(update: Update, context: Any, wid: str) -> bool:
    return await forward_command_handler(update, context, pinned_wid=wid)


async def _run_photo(update: Update, context: Any, wid: str) -> bool:
    return await photo_handler(update, context, pinned_wid=wid)


async def _run_document(update: Update, context: Any, wid: str) -> bool:
    return await document_handler(update, context, pinned_wid=wid)


async def _run_voice(update: Update, context: Any, wid: str) -> bool:
    return await voice_handler(update, context, pinned_wid=wid, ordered=True)


async def _run_unsupported(update: Update, context: Any, wid: str) -> bool:
    return await unsupported_content_handler(update, context, pinned_wid=wid)


async def _queue_notice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, wid: str, ahead: int
) -> None:
    user = update.effective_user
    if update.message is None or user is None:
        return
    sess = session_manager.find_session_by_window(wid)
    label = (
        (sess.name or sess.id)
        if sess is not None
        else session_manager.get_display_name(wid)
    )
    try:
        await safe_reply(
            update.message,
            t(user.id, "queue.accepted", session=label, ahead=ahead),
        )
    except Exception:
        return


def _enqueue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    processor: InboundProcessor,
    target_window_id: str | None = None,
) -> bool:
    user = update.effective_user
    if user is None or update.message is None or not is_user_allowed(user.id):
        return False
    wid = target_window_id or active_window(user.id)
    if wid is None:
        return False
    receipt = enqueue_inbound(
        user.id,
        wid,
        update,
        context,
        kind=kind,
        processor=processor,
    )
    if receipt.ahead:
        asyncio.create_task(
            _queue_notice(update, context, wid, receipt.ahead),
            name=f"inbound-ack:{user.id}:{wid}:{receipt.entry.sequence}",
        )
    return True


async def text_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    user = update.effective_user
    if user is not None and get_flow(user.id) is not None:
        return await text_handler(update, context)
    target_wid = None
    if user is not None and update.message is not None:
        reply = getattr(update.message, "reply_to_message", None)
        target_sid = (
            lookup_session_for_message(user.id, reply.message_id)
            if reply is not None
            else None
        )
        target = session_manager.get_session(target_sid) if target_sid else None
        if (
            target is not None
            and target.window_id
            and target.state in ("active", "idle")
        ):
            target_wid = target.window_id
    if _enqueue(
        update,
        context,
        kind="text",
        processor=_run_text,
        target_window_id=target_wid,
    ):
        return True
    return await text_handler(update, context)


async def command_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _enqueue(update, context, kind="command", processor=_run_command):
        return True
    return await forward_command_handler(update, context)


async def photo_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _enqueue(update, context, kind="photo", processor=_run_photo):
        return True
    return await photo_handler(update, context)


async def document_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _enqueue(update, context, kind="document", processor=_run_document):
        return True
    return await document_handler(update, context)


async def voice_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _enqueue(update, context, kind="voice", processor=_run_voice):
        return True
    return await voice_handler(update, context)


async def unsupported_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _enqueue(update, context, kind="unsupported", processor=_run_unsupported):
        return True
    return await unsupported_content_handler(update, context)


__all__ = [
    "command_intake_handler",
    "document_intake_handler",
    "photo_intake_handler",
    "text_intake_handler",
    "unsupported_intake_handler",
    "voice_intake_handler",
]
