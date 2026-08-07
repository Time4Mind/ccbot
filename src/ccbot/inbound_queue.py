"""Per-session FIFO for Telegram content updates.

Handlers registered with python-telegram-bot only pin and enqueue an update.
One background worker per ``(user, window)`` runs the real content handlers in
arrival order, leaving callback queries and other sessions responsive.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telegram import Update

logger = logging.getLogger(__name__)

InboundProcessor = Callable[[Update, Any, str], Awaitable[bool]]


@dataclass
class InboundEntry:
    update: Update
    context: Any
    target_window_id: str
    kind: str
    sequence: int
    processor: InboundProcessor
    completion: asyncio.Future[bool]


@dataclass
class InboundReceipt:
    entry: InboundEntry
    ahead: int

    @property
    def completion(self) -> asyncio.Future[bool]:
        return self.entry.completion


@dataclass
class _Lane:
    user_id: int
    window_id: str
    entries: deque[InboundEntry] = field(default_factory=deque)
    next_sequence: int = 1
    active: InboundEntry | None = None
    worker: asyncio.Task[None] | None = None


_lanes: dict[tuple[int, str], _Lane] = {}


def enqueue_inbound(
    user_id: int,
    window_id: str,
    update: Update,
    context: Any,
    *,
    kind: str,
    processor: InboundProcessor,
) -> InboundReceipt:
    """Pin one update to ``window_id`` and return without yielding."""
    key = (user_id, window_id)
    lane = _lanes.get(key)
    if lane is None:
        lane = _Lane(user_id=user_id, window_id=window_id)
        _lanes[key] = lane

    loop = asyncio.get_running_loop()
    ahead = len(lane.entries) + (1 if lane.active is not None else 0)
    entry = InboundEntry(
        update=update,
        context=context,
        target_window_id=window_id,
        kind=kind,
        sequence=lane.next_sequence,
        processor=processor,
        completion=loop.create_future(),
    )
    lane.next_sequence += 1
    lane.entries.append(entry)
    logger.info(
        "inbound_enqueued",
        extra={
            "user_id": user_id,
            "window_id": window_id,
            "sequence": entry.sequence,
            "kind": kind,
            "ahead": ahead,
            "pending": len(lane.entries),
        },
    )
    if lane.worker is None or lane.worker.done():
        lane.worker = loop.create_task(
            _drain_lane(key, lane),
            name=f"inbound-queue:{user_id}:{window_id}",
        )
    return InboundReceipt(entry=entry, ahead=ahead)


async def _drain_lane(key: tuple[int, str], lane: _Lane) -> None:
    try:
        while lane.entries:
            entry = lane.entries.popleft()
            lane.active = entry
            delivered = False
            try:
                delivered = await entry.processor(
                    entry.update, entry.context, entry.target_window_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "inbound_delivery_failed",
                    extra={
                        "user_id": lane.user_id,
                        "window_id": lane.window_id,
                        "sequence": entry.sequence,
                        "kind": entry.kind,
                        "error": str(exc),
                    },
                )
            finally:
                if not entry.completion.done():
                    entry.completion.set_result(delivered)
                lane.active = None
            logger.info(
                "inbound_delivered",
                extra={
                    "user_id": lane.user_id,
                    "window_id": lane.window_id,
                    "sequence": entry.sequence,
                    "kind": entry.kind,
                    "delivered": delivered,
                    "remaining": len(lane.entries),
                },
            )
    finally:
        if _lanes.get(key) is lane and not lane.entries:
            _lanes.pop(key, None)
        lane.worker = None


def pending_inbound_count(user_id: int, window_id: str) -> int:
    lane = _lanes.get((user_id, window_id))
    if lane is None:
        return 0
    return len(lane.entries) + (1 if lane.active is not None else 0)


async def shutdown_inbound_queues() -> None:
    """Cancel workers and resolve queued receipts during bot shutdown."""
    lanes = list(_lanes.values())
    tasks = [lane.worker for lane in lanes if lane.worker is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for lane in lanes:
        pending = list(lane.entries)
        if lane.active is not None:
            pending.append(lane.active)
        for entry in pending:
            if not entry.completion.done():
                entry.completion.set_result(False)
    _lanes.clear()


def reset_inbound_queues_for_test() -> None:
    for lane in _lanes.values():
        if lane.worker is not None and not lane.worker.done():
            lane.worker.cancel()
    _lanes.clear()


__all__ = [
    "InboundEntry",
    "InboundReceipt",
    "enqueue_inbound",
    "pending_inbound_count",
    "reset_inbound_queues_for_test",
    "shutdown_inbound_queues",
]
