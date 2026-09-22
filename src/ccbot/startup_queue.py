"""Ordered inbound queue for a session-creation flow.

The queue starts when the user opens the new-session picker, before the first
filesystem/UI await.  A high-priority Telegram handler then captures every
message while the picker is open or the agent TUI is booting.  Once the new
tmux window is genuinely ready, messages are replayed through the normal
handlers in Telegram order.

Every replay is pinned to the window created for the flow. A failed entry is
reported by its regular handler and removed so it cannot invisibly stall the
rest of the queue.
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
    session_id: str | None = None
    window_id: str | None = None
    drain_task: asyncio.Task[None] | None = None
    operation_task: asyncio.Task[None] | None = None


_flows: dict[int, StartupFlow] = {}
_CONTROL_COMMANDS = frozenset(
    {"archive", "health", "help", "kill", "login", "menu", "new", "stop", "usage"}
)


def begin_startup_queue(user_id: int) -> StartupFlow:
    """Open (or retain) the queue for the user's in-progress new session."""
    flow = _flows.get(user_id)
    if flow is None or flow.session_id is not None or flow.window_id is not None:
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
    return len(fail_startup_queue(user_id))


def fail_startup_queue(
    user_id: int,
    *,
    flow: StartupFlow | None = None,
    window_id: str | None = None,
) -> list[QueuedInbound]:
    """Close a failed flow and return every undelivered update in FIFO order."""
    target = flow or _flows.get(user_id)
    if target is None or (window_id is not None and target.window_id != window_id):
        return []
    if _flows.get(user_id) is target:
        _flows.pop(user_id, None)
    current = asyncio.current_task()
    if (
        target.drain_task is not None
        and target.drain_task is not current
        and not target.drain_task.done()
    ):
        target.drain_task.cancel()
    if (
        target.operation_task is not None
        and target.operation_task is not current
        and not target.operation_task.done()
    ):
        target.operation_task.cancel()
    entries = list(target.entries)
    target.entries.clear()
    logger.info("startup queue cancelled user=%d pending=%d", user_id, len(entries))
    return entries


async def report_failed_startup_entries(entries: list[QueuedInbound]) -> None:
    """Tell the user that each prompt owned by a failed flow was not sent."""
    from .handlers.message_sender import safe_send
    from .i18n import t

    for entry in entries:
        message = entry.update.message
        user = entry.update.effective_user
        if message is None or user is None:
            continue
        if message.text:
            label = message.text
        elif message.voice:
            label = "voice message"
        elif message.photo:
            label = "photo"
        elif message.document:
            label = "document"
        else:
            label = "message"
        await safe_send(
            entry.context.bot,
            user.id,
            t(user.id, "startup.prompt_not_sent", prompt=label),
        )


def track_startup_operation(user_id: int, task: asyncio.Task[None]) -> None:
    """Attach cancellable session creation work to the user's open flow."""
    flow = _flows.get(user_id)
    if flow is not None:
        flow.operation_task = task


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
    if flow.session_id is not None:
        from .session import session_manager

        active = session_manager.get_active_session(user.id)
        if active is None or active.id != flow.session_id:
            return
    # The directory naming screen owns the next text message. This handler is
    # registered earlier than the normal text router, so capturing it here
    # would silently queue the folder name as a future agent prompt.
    from .handlers.directory_browser import STATE_KEY, STATE_NAMING_DIRECTORY

    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state == STATE_NAMING_DIRECTORY and update.message.text is not None:
        return
    text = (update.message.text or "").strip()
    command = (
        text.split(maxsplit=1)[0].removeprefix("/").split("@", 1)[0].casefold()
        if text
        else ""
    )
    if text.startswith("/") and command in _CONTROL_COMMANDS:
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
    if flow.session_id is not None:
        _surface_startup_entry(entry, session_id=flow.session_id)
    elif flow.window_id is not None:
        _surface_startup_entry(entry, window_id=flow.window_id)
    logger.info(
        "startup queue captured user=%d seq=%d message_id=%s pending=%d",
        user.id,
        entry.sequence,
        getattr(update.message, "message_id", None),
        len(flow.entries),
    )
    return entry


def _surface_startup_entry(
    entry: QueuedInbound, *, session_id: str = "", window_id: str = ""
) -> None:
    """Render an immediate card receipt while delivery waits for readiness."""
    from .handlers.card_types import PendingPrompt
    from .handlers.notifications import get_card_state, schedule_card_after_message
    from .session import session_manager

    message = entry.update.message
    user = entry.update.effective_user
    if message is None or user is None:
        return
    sess = (
        session_manager.get_session(session_id)
        if session_id
        else session_manager.find_session_by_window(window_id)
    )
    if sess is None:
        return
    state = get_card_state(user.id, sess)
    message_id = message.message_id
    text = message.text or ""
    if text and not text.startswith("/"):
        request_id = str(message_id)
        if not any(row.request_id == request_id for row in state.pending_prompts):
            state.pending_prompts.append(
                PendingPrompt(request_id=request_id, text=text, user_icon="👤")
            )
        if not any(mid == message_id for mid, _seq in state.pending_request_sequences):
            state.next_request_sequence += 1
            state.pending_request_sequences.append(
                (message_id, state.next_request_sequence)
            )
    state.current_page_idx = None
    schedule_card_after_message(
        entry.context.bot,
        user.id,
        sess,
        message_id,
    )


def _discard_startup_receipt(entry: QueuedInbound, window_id: str) -> None:
    """Remove a synthetic prompt when its queued delivery fails."""
    from .handlers.notifications import get_card_state
    from .session import session_manager

    message = entry.update.message
    if message is None:
        return
    sess = session_manager.find_session_by_window(window_id)
    if sess is None:
        return
    user = entry.update.effective_user
    if user is None:
        return
    state = get_card_state(user.id, sess)
    message_id = message.message_id
    state.pending_prompts = [
        row for row in state.pending_prompts if row.request_id != str(message_id)
    ]
    state.pending_request_sequences = [
        item for item in state.pending_request_sequences if item[0] != message_id
    ]


async def _replay(entry: QueuedInbound, window_id: str | None = None) -> bool:
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
    pinned = {"pinned_wid": window_id} if window_id is not None else {}
    if message.voice:
        if window_id is None:
            result = await voice_handler(entry.update, entry.context)
        else:
            result = await voice_handler(
                entry.update,
                entry.context,
                pinned_wid=window_id,
                ordered=True,
            )
    elif message.photo:
        result = await photo_handler(entry.update, entry.context, **pinned)
    elif message.document:
        result = await document_handler(entry.update, entry.context, **pinned)
    elif message.text:
        if message.text.startswith("/"):
            result = await forward_command_handler(
                entry.update, entry.context, **pinned
            )
        else:
            result = await text_handler(entry.update, entry.context, **pinned)
    else:
        result = await unsupported_content_handler(
            entry.update, entry.context, **pinned
        )
    # Legacy handlers returned None on success.  New delivery-aware paths
    # return a bool; preserve compatibility while they are migrated.
    return result is not False


async def _drain(user_id: int, window_id: str, flow: StartupFlow) -> None:
    from .session import session_manager

    try:
        session = session_manager.find_session_by_window(window_id)
        if session is not None and getattr(session, "node_id", "local") != "local":
            # The worker RPC only returns after its agent prompt is ready.
            ready = True
        else:
            ready = await session_manager.wait_for_window_ready(window_id)
        if not ready:
            logger.error(
                "startup queue failed: window never became ready "
                "user=%d window=%s pending=%d",
                user_id,
                window_id,
                len(flow.entries),
            )
            entries = fail_startup_queue(user_id, flow=flow, window_id=window_id)
            for entry in entries:
                _discard_startup_receipt(entry, window_id)
            await report_failed_startup_entries(entries)
            return
        while flow.entries:
            entry = flow.entries[0]
            try:
                delivered = await _replay(entry, window_id)
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
                _discard_startup_receipt(entry, window_id)
                flow.entries.popleft()
                continue
            if not delivered:
                logger.error(
                    "startup queue item failed; continuing user=%d window=%s "
                    "seq=%d pending=%d",
                    user_id,
                    window_id,
                    entry.sequence,
                    len(flow.entries),
                )
                _discard_startup_receipt(entry, window_id)
                flow.entries.popleft()
                continue
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


def bind_startup_queue(
    user_id: int,
    window_id: str,
    *,
    flow: StartupFlow | None = None,
) -> asyncio.Task[None] | None:
    """Bind the current flow to its new window and start ordered draining."""
    target = flow or _flows.get(user_id)
    if target is None:
        return None
    target.window_id = window_id
    if target.session_id is None:
        for entry in target.entries:
            _surface_startup_entry(entry, window_id=window_id)
    if target.drain_task is not None and not target.drain_task.done():
        return target.drain_task
    target.drain_task = asyncio.create_task(
        _drain(user_id, window_id, target),
        name=f"startup-queue:{user_id}:{window_id}",
    )
    return target.drain_task


def bind_startup_session(user_id: int, session_id: str) -> StartupFlow | None:
    """Attach the provisional card so queued prompts are visible immediately."""
    flow = _flows.get(user_id)
    if flow is None:
        return None
    flow.session_id = session_id
    for entry in flow.entries:
        _surface_startup_entry(entry, session_id=session_id)
    return flow


def reset_startup_queues_for_test() -> None:
    """Test-only cleanup for the module-global registry."""
    for flow in _flows.values():
        if flow.drain_task is not None and not flow.drain_task.done():
            flow.drain_task.cancel()
        if flow.operation_task is not None and not flow.operation_task.done():
            flow.operation_task.cancel()
    _flows.clear()


__all__ = [
    "begin_startup_queue",
    "bind_startup_session",
    "bind_startup_queue",
    "cancel_startup_queue",
    "capture_startup_message",
    "enqueue_startup_message",
    "fail_startup_queue",
    "has_startup_queue",
    "pending_startup_count",
    "report_failed_startup_entries",
    "track_startup_operation",
]
