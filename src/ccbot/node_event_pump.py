"""Supervised worker-to-leader session event delivery."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .node_transport import NodeEnvelope, NodeTransport

logger = logging.getLogger(__name__)


class NodeEventPump:
    """Poll worker transcripts and retain events until relay send succeeds."""

    def __init__(self, node_id: str, *, available: bool = True):
        self.node_id = node_id
        self.pending: deque[dict[str, Any]] = deque()
        self.healthy = available

    async def run(
        self,
        poll_events: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        transport: Callable[[], NodeTransport],
        publish_health: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                if not self.pending:
                    self.pending.extend(await poll_events())
                while self.pending:
                    payload = self.pending[0]
                    await transport().send(
                        NodeEnvelope(
                            kind="event",
                            payload={"node_id": self.node_id, **payload},
                        )
                    )
                    self.pending.popleft()
                recovered = not self.healthy
                self.healthy = True
                if recovered:
                    logger.info("node event pump recovered node=%s", self.node_id)
                    await publish_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.healthy = False
                payload = self.pending[0] if self.pending else {}
                logger.exception(
                    "node event pump failed node=%s session=%s pending=%d: %s",
                    self.node_id,
                    payload.get("session_id", ""),
                    len(self.pending),
                    exc,
                )
                try:
                    await publish_health()
                except Exception:
                    logger.debug(
                        "node event pump could not publish degraded health node=%s",
                        self.node_id,
                    )


__all__ = ["NodeEventPump"]
