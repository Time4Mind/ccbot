"""FIFO queue for prompts received while a context transfer is starting.

The queue is deliberately separate from the existing new-session queue. A
transfer has no local tmux window yet, and its eventual delivery may be a
remote node runtime rather than ``send_to_window``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from telegram import Update

logger = logging.getLogger(__name__)

TransferDelivery = Callable[[Update, Any], Awaitable[bool]]


@dataclass
class QueuedTransferRequest:
    update: Update
    context: Any
    sequence: int


@dataclass
class TransferFlow:
    user_id: int
    transfer_id: str
    entries: deque[QueuedTransferRequest] = field(default_factory=deque)
    next_sequence: int = 1
    drain_task: asyncio.Task[None] | None = None
    delivery: TransferDelivery | None = None


_flows: dict[int, TransferFlow] = {}


def begin_transfer_queue(user_id: int, transfer_id: str) -> TransferFlow:
    """Open or retain the queue for one user's in-progress transfer."""
    flow = _flows.get(user_id)
    if flow is None or flow.transfer_id != transfer_id:
        flow = TransferFlow(user_id=user_id, transfer_id=transfer_id)
        _flows[user_id] = flow
    return flow


def has_transfer_queue(user_id: int) -> bool:
    return user_id in _flows


def pending_transfer_count(user_id: int) -> int:
    flow = _flows.get(user_id)
    return len(flow.entries) if flow is not None else 0


def cancel_transfer_queue(user_id: int) -> int:
    """Drop a transfer queue and return the number of unsent requests."""
    flow = _flows.pop(user_id, None)
    if flow is None:
        return 0
    if flow.drain_task is not None and not flow.drain_task.done():
        flow.drain_task.cancel()
    return len(flow.entries)


def capture_transfer_message(update: Update, context: Any) -> bool:
    """Capture a normal prompt while transfer startup is in progress.

    Navigation and recovery commands stay in the ordinary router. They must
    remain usable when a transfer is unavailable or fails.
    """
    user = update.effective_user
    message = update.message
    if user is None or message is None:
        return False
    flow = _flows.get(user.id)
    if flow is None:
        return False
    text = (getattr(message, "text", None) or "").strip()
    if text.startswith(("/menu", "/new", "/login")):
        return False
    entry = QueuedTransferRequest(
        update=update,
        context=context,
        sequence=flow.next_sequence,
    )
    flow.next_sequence += 1
    flow.entries.append(entry)
    logger.info(
        "transfer queue captured user=%d transfer=%s seq=%d pending=%d",
        user.id,
        flow.transfer_id,
        entry.sequence,
        len(flow.entries),
    )
    return True


async def _drain(user_id: int, flow: TransferFlow) -> None:
    try:
        while flow.entries and flow.delivery is not None:
            entry = flow.entries.popleft()
            try:
                delivered = await flow.delivery(entry.update, entry.context)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "transfer queue delivery failed user=%d transfer=%s seq=%d",
                    user_id,
                    flow.transfer_id,
                    entry.sequence,
                )
                delivered = False
            if not delivered:
                logger.error(
                    "transfer queue item rejected user=%d transfer=%s seq=%d",
                    user_id,
                    flow.transfer_id,
                    entry.sequence,
                )
    finally:
        if _flows.get(user_id) is flow:
            if flow.entries and flow.delivery is None:
                flow.drain_task = None
            else:
                _flows.pop(user_id, None)
        flow.drain_task = None


def bind_transfer_queue(
    user_id: int, delivery: TransferDelivery
) -> asyncio.Task[None] | None:
    """Mark the target ready and start ordered delivery."""
    flow = _flows.get(user_id)
    if flow is None:
        return None
    flow.delivery = delivery
    if flow.drain_task is not None and not flow.drain_task.done():
        return flow.drain_task
    flow.drain_task = asyncio.create_task(
        _drain(user_id, flow), name=f"transfer-queue:{user_id}:{flow.transfer_id}"
    )
    return flow.drain_task


def reset_transfer_queues_for_test() -> None:
    for flow in _flows.values():
        if flow.drain_task is not None and not flow.drain_task.done():
            flow.drain_task.cancel()
    _flows.clear()


__all__ = [
    "begin_transfer_queue",
    "bind_transfer_queue",
    "cancel_transfer_queue",
    "capture_transfer_message",
    "has_transfer_queue",
    "pending_transfer_count",
    "reset_transfer_queues_for_test",
]
