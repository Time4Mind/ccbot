"""Short-lived in-memory FIFO for prompts waiting on a remote node."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .handlers.message_sender import safe_edit, safe_reply

REMOTE_PROMPT_TTL_SECONDS = 15 * 60
REMOTE_PROMPT_QUEUE_LIMIT = 10

Delivery = Callable[[], Awaitable[bool]]
NodeAvailable = Callable[[str], bool]


@dataclass
class QueuedRemotePrompt:
    session_id: str
    node_id: str
    node_name: str
    created_at: float
    deliver: Delivery
    receipt: Any = None


@dataclass
class RemotePromptFlow:
    session_id: str
    node_id: str
    node_name: str
    entries: deque[QueuedRemotePrompt] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None


def _default_node_available(node_id: str) -> bool:
    from .session import session_manager

    node = session_manager.get_node(node_id)
    return node is not None and node.is_available()


class RemotePromptQueue:
    """Bounded, process-local queue with visible per-message receipts."""

    def __init__(
        self,
        *,
        node_available: NodeAvailable = _default_node_available,
        autostart: bool = True,
        now: Callable[[], float] = time.time,
        poll_interval: float = 2.0,
    ) -> None:
        self._node_available = node_available
        self._autostart = autostart
        self._now = now
        self._poll_interval = poll_interval
        self._flows: dict[str, RemotePromptFlow] = {}

    def has_pending(self, session_id: str) -> bool:
        flow = self._flows.get(session_id)
        return bool(flow and flow.entries)

    async def admit(
        self,
        *,
        original_message: Any,
        session_id: str,
        node_id: str,
        node_name: str,
        deliver: Delivery,
    ) -> bool:
        flow = self._flows.get(session_id)
        if flow is None:
            flow = RemotePromptFlow(
                session_id=session_id,
                node_id=node_id,
                node_name=node_name,
            )
            self._flows[session_id] = flow
        if len(flow.entries) >= REMOTE_PROMPT_QUEUE_LIMIT:
            await safe_reply(
                original_message,
                f"⛔ В очереди на {node_name} уже 10 запросов. "
                "Дождись отправки хотя бы одного.",
            )
            return False
        position = len(flow.entries) + 1
        entry = QueuedRemotePrompt(
            session_id=session_id,
            node_id=node_id,
            node_name=node_name,
            created_at=self._now(),
            deliver=deliver,
        )
        flow.entries.append(entry)
        entry.receipt = await safe_reply(
            original_message,
            f"⏳ В очереди на {node_name} · позиция {position} · ждём до 15 минут",
        )
        if self._autostart and (flow.task is None or flow.task.done()):
            flow.task = asyncio.create_task(
                self._run(session_id), name=f"remote-prompt-queue:{session_id}"
            )
        return True

    async def drain_once(self, session_id: str) -> int:
        flow = self._flows.get(session_id)
        if flow is None:
            return 0
        now = self._now()
        kept: deque[QueuedRemotePrompt] = deque()
        while flow.entries:
            entry = flow.entries.popleft()
            if now - entry.created_at >= REMOTE_PROMPT_TTL_SECONDS:
                if entry.receipt is not None:
                    await safe_edit(
                        entry.receipt,
                        f"❌ Не отправлено: {entry.node_name} недоступна более 15 минут",
                    )
            else:
                kept.append(entry)
        flow.entries = kept
        delivered = 0
        while flow.entries and self._node_available(flow.node_id):
            entry = flow.entries[0]
            success = await entry.deliver()
            if not success and not self._node_available(flow.node_id):
                break
            flow.entries.popleft()
            if entry.receipt is not None:
                await safe_edit(
                    entry.receipt,
                    (
                        f"✅ Передано на {entry.node_name}"
                        if success
                        else f"❌ Не отправлено на {entry.node_name}"
                    ),
                )
            if success:
                delivered += 1
        if not flow.entries:
            self._flows.pop(session_id, None)
        return delivered

    async def _run(self, session_id: str) -> None:
        try:
            while self.has_pending(session_id):
                await self.drain_once(session_id)
                if self.has_pending(session_id):
                    await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise
        finally:
            flow = self._flows.get(session_id)
            if flow is not None:
                flow.task = None

    def reset(self) -> None:
        for flow in self._flows.values():
            if flow.task is not None and not flow.task.done():
                flow.task.cancel()
        self._flows.clear()


remote_prompt_queue = RemotePromptQueue()


__all__ = [
    "REMOTE_PROMPT_QUEUE_LIMIT",
    "REMOTE_PROMPT_TTL_SECONDS",
    "RemotePromptQueue",
    "remote_prompt_queue",
]
