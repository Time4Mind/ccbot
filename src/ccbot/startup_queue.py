"""Ordered inbound queue for a session-creation flow.

The queue starts when the user opens the new-session picker, before the first
filesystem/UI await.  A high-priority Telegram handler then captures every
message while the picker is open or the agent TUI is booting.  Once the new
tmux window is genuinely ready, messages are replayed through the normal
handlers in Telegram order.

Entries are removed only after their handler reports success.  A failed entry
and everything behind it stay queued, so a transient delivery failure cannot
silently turn into message loss.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)


@dataclass
class QueuedInbound:
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    sequence: int


@dataclass
class StartupFlow:
    user_id: int
    entries: deque[QueuedInbound] = field(default_factory=deque)
    next_sequence: int = 1
    window_id: str | None = None
    drain_task: asyncio.Task[None] | None = None


_flows: dict[int, StartupFlow] = {}


def begin_startup_queue(user_id: int) -> StartupFlow:
    """Open (or retain) the queue for the user's in-progress new session."""
    flow = _flows.get(user_id)
    if flow is None:
        flow = StartupFlow(user_id=user_id)
        _flows[user_id] = flow
        logger.info("startup queue opened user=%d", user_id)
    return flow


def has_startup_queue(user_id: int) -> bool:
    return user_id in _flows


def pending_startup_count(user_id: int) -> int:
    flow = _flows.get(user_id)
    return len(flow.entries) if flow is not None else 0


def cancel_startup_queue(user_id: int) -> int:
    """Explicitly abandon a cancelled flow and return its unsent count."""
    flow = _flows.pop(user_id, None)
    if flow is None:
        return 0
    if flow.drain_task is not None and not flow.drain_task.done():
        flow.drain_task.cancel()
    count = len(flow.entries)
    logger.info("startup queue cancelled user=%d pending=%d", user_id, count)
    return count


async def capture_startup_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """High-priority PTB handler that captures messages during Start.

    Raising :class:`ApplicationHandlerStop` prevents the normal handler from
    routing the same update to the previously-active session.
    """
    user = update.effective_user
    if user is None or update.message is None:
        return
    flow = _flows.get(user.id)
    if flow is None:
        return
    text = (update.message.text or "").strip()
    if text.startswith(("/login", "/new")):
        # Control-plane commands must be able to repair/restart a failed
        # creation flow. begin_startup_queue() retains the existing entries.
        return
    if text and not text.startswith("/"):
        # Authentication codes are control-plane input, never agent prompts.
        # Let the normal text handler consume them while retaining the queued
        # user turns for the next successful Start attempt.
        from .codex_auth import get_flow

        if get_flow(user.id) is not None:
            return
    enqueue_startup_message(update, context)
    raise ApplicationHandlerStop


def enqueue_startup_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> QueuedInbound | None:
    """Append an update to an already-open flow without stopping dispatch."""
    user = update.effective_user
    if user is None or update.message is None:
        return None
    flow = _flows.get(user.id)
    if flow is None:
        return None
    entry = QueuedInbound(update=update, context=context, sequence=flow.next_sequence)
    flow.next_sequence += 1
    flow.entries.append(entry)
    logger.info(
        "startup queue captured user=%d seq=%d message_id=%s pending=%d",
        user.id,
        entry.sequence,
        getattr(update.message, "message_id", None),
        len(flow.entries),
    )
    return entry


async def _replay(entry: QueuedInbound) -> bool:
    """Replay one captured update through its regular inbound handler."""
    # Lazy import avoids a cycle: bot.messages imports the capture handler for
    # application registration.
    from .bot.messages import (
        document_handler,
        forward_command_handler,
        photo_handler,
        text_handler,
        unsupported_content_handler,
        voice_handler,
    )

    message = entry.update.message
    if message is None:
        return True
    if message.voice:
        result = await voice_handler(entry.update, entry.context)
    elif message.photo:
        result = await photo_handler(entry.update, entry.context)
    elif message.document:
        result = await document_handler(entry.update, entry.context)
    elif message.text:
        if message.text.startswith("/"):
            result = await forward_command_handler(entry.update, entry.context)
        else:
            result = await text_handler(entry.update, entry.context)
    else:
        result = await unsupported_content_handler(entry.update, entry.context)
    # Legacy handlers returned None on success.  New delivery-aware paths
    # return a bool; preserve compatibility while they are migrated.
    return result is not False


async def _drain(user_id: int, window_id: str) -> None:
    from .session import session_manager

    flow = _flows.get(user_id)
    if flow is None:
        return
    try:
        ready = await session_manager.wait_for_window_ready(window_id)
        if not ready:
            logger.error(
                "startup queue kept closed: window never became ready "
                "user=%d window=%s pending=%d",
                user_id,
                window_id,
                len(flow.entries),
            )
            return
        while flow.entries:
            entry = flow.entries[0]
            try:
                delivered = await _replay(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "startup queue replay failed user=%d window=%s seq=%d: %s",
                    user_id,
                    window_id,
                    entry.sequence,
                    exc,
                )
                return
            if not delivered:
                logger.error(
                    "startup queue delivery unconfirmed user=%d window=%s "
                    "seq=%d pending=%d",
                    user_id,
                    window_id,
                    entry.sequence,
                    len(flow.entries),
                )
                return
            flow.entries.popleft()
            logger.info(
                "startup queue delivered user=%d window=%s seq=%d remaining=%d",
                user_id,
                window_id,
                entry.sequence,
                len(flow.entries),
            )
        # No await between the empty check and removal. A capture either
        # appended before this point and was drained, or observes no flow and
        # follows the now-active session's normal delivery path.
        if _flows.get(user_id) is flow and not flow.entries:
            _flows.pop(user_id, None)
            logger.info("startup queue drained user=%d window=%s", user_id, window_id)
    finally:
        if _flows.get(user_id) is flow:
            flow.drain_task = None


def bind_startup_queue(user_id: int, window_id: str) -> asyncio.Task[None] | None:
    """Bind the current flow to its new window and start ordered draining."""
    flow = _flows.get(user_id)
    if flow is None:
        return None
    flow.window_id = window_id
    if flow.drain_task is not None and not flow.drain_task.done():
        return flow.drain_task
    flow.drain_task = asyncio.create_task(
        _drain(user_id, window_id), name=f"startup-queue:{user_id}:{window_id}"
    )
    return flow.drain_task


def reset_startup_queues_for_test() -> None:
    """Test-only cleanup for the module-global registry."""
    for flow in _flows.values():
        if flow.drain_task is not None and not flow.drain_task.done():
            flow.drain_task.cancel()
    _flows.clear()


__all__ = [
    "begin_startup_queue",
    "bind_startup_queue",
    "cancel_startup_queue",
    "capture_startup_message",
    "enqueue_startup_message",
    "has_startup_queue",
    "pending_startup_count",
]
