"""Fast Telegram intake handlers backed by the per-session FIFO.

Each callback pins the active tmux window before its first await, enqueues the
update, and returns. The real handlers run in a background lane, so a slow
voice transcription never blocks session-switch callbacks.
"""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..codex_auth import get_flow
from ..default_session import claim_default_session
from ..handlers.notifications import (
    get_card_state,
    lookup_session_for_message,
    schedule_card_after_message,
)
from ..handlers.card_types import PendingPrompt
from ..handlers.directory_browser import (
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
    STATE_PREPROCESSING_INSTRUCTION,
)
from ..inbound_queue import InboundProcessor, enqueue_inbound
from ..session import session_manager
from ..transfer_queue import capture_transfer_message
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
    return await voice_handler(
        update,
        context,
        pinned_wid=wid,
        ordered=True,
        surface_pending=False,
    )


async def _run_unsupported(update: Update, context: Any, wid: str) -> bool:
    return await unsupported_content_handler(update, context, pinned_wid=wid)


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
    sess = session_manager.find_session_by_window(wid)
    if sess is not None:
        # Promotion is deliberately synchronous and happens before the FIFO
        # owns the update. A session switch immediately after this point can
        # never redirect the request or make a second message claim it.
        claim_default_session(context.bot, user.id, sess)
    receipt = enqueue_inbound(
        user.id,
        wid,
        update,
        context,
        kind=kind,
        processor=processor,
    )
    if sess is not None:
        # A new Telegram request owns the move to the latest page. Do it now,
        # before async queue work: a later pagination tap must stay authoritative
        # even if this request's transcript event arrives after that tap.
        state = get_card_state(user.id, sess)
        message_id = update.message.message_id
        if kind != "command":
            # Slash commands are control-plane input. Claude records commands
            # such as /model as ``local_command`` rather than ``user_msg``, so
            # they must not reserve a conversational receipt that the next
            # real prompt would consume.
            pending_sequences = getattr(state, "pending_request_sequences", None)
            if pending_sequences is None:
                pending_sequences = []
                state.pending_request_sequences = pending_sequences
            existing = next(
                (
                    sequence
                    for queued_message_id, sequence in pending_sequences
                    if queued_message_id == message_id
                ),
                None,
            )
            if existing is None:
                state.next_request_sequence = (
                    getattr(state, "next_request_sequence", 0) + 1
                )
                request_sequence = state.next_request_sequence
                pending_sequences.append((message_id, request_sequence))
            else:
                request_sequence = existing

            def _discard_failed_request(done: object) -> None:
                try:
                    delivered = bool(done.result())  # type: ignore[attr-defined]
                except Exception:
                    delivered = False
                if delivered:
                    return
                state.pending_request_sequences = [
                    item
                    for item in state.pending_request_sequences
                    if item != (message_id, request_sequence)
                ]
                pending_prompts = getattr(state, "pending_prompts", [])
                state.pending_prompts = [
                    row for row in pending_prompts if row.request_id != str(message_id)
                ]

            completion = getattr(receipt, "completion", None)
            if completion is not None:
                completion.add_done_callback(_discard_failed_request)
        if kind == "text" and update.message.text:
            request_id = str(message_id)
            pending_prompts = getattr(state, "pending_prompts", None)
            if pending_prompts is None:
                pending_prompts = []
                state.pending_prompts = pending_prompts
            if not any(row.request_id == request_id for row in pending_prompts):
                pending_prompts.append(
                    PendingPrompt(
                        request_id=request_id,
                        text=update.message.text,
                        user_icon="👤",
                    )
                )
        state.current_page_idx = None
        if kind == "voice":
            state.voice_pending = True
        schedule_card_after_message(
            context.bot,
            user.id,
            sess,
            update.message.message_id,
        )
    return True


def _capture_pending_transfer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Keep requests off the source session while a target is starting."""
    return capture_transfer_message(update, context)


async def text_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    user = update.effective_user
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state in (STATE_NAMING_DIRECTORY, STATE_PREPROCESSING_INSTRUCTION):
        # Folder names are control-plane input. Do not pin them to the active
        # session or schedule its card below the directory browser.
        return await text_handler(update, context)
    if _capture_pending_transfer(update, context):
        return True
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
    if _capture_pending_transfer(update, context):
        return True
    if _enqueue(update, context, kind="command", processor=_run_command):
        return True
    return await forward_command_handler(update, context)


async def photo_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _capture_pending_transfer(update, context):
        return True
    if _enqueue(update, context, kind="photo", processor=_run_photo):
        return True
    return await photo_handler(update, context)


async def document_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _capture_pending_transfer(update, context):
        return True
    if _enqueue(update, context, kind="document", processor=_run_document):
        return True
    return await document_handler(update, context)


async def voice_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _capture_pending_transfer(update, context):
        return True
    if _enqueue(update, context, kind="voice", processor=_run_voice):
        return True
    return await voice_handler(update, context)


async def unsupported_intake_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if _capture_pending_transfer(update, context):
        return True
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
