"""Non-blocking leader RPC event intake, apply, acknowledgement, and replay."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .node_event_state import LeaderEventCursorStore
from .node_transport import NodeEnvelope, NodeTransport

logger = logging.getLogger(__name__)
RpcEventHandler = Callable[[NodeEnvelope], Awaitable[None]]
TransportFactory = Callable[[], Awaitable[NodeTransport]]
_RECONNECT_INITIAL_SECONDS = 0.5
_RECONNECT_MAX_SECONDS = 5.0


class NodeRpcEventMixin:
    _closed: bool
    _transport: NodeTransport
    _reconnect: TransportFactory | None
    _event_handler: RpcEventHandler | None
    _event_cursors: LeaderEventCursorStore
    _event_queue: asyncio.Queue[NodeEnvelope]
    _health_queue: asyncio.Queue[NodeEnvelope]
    _pending: dict[str, asyncio.Future[dict[str, Any]]]

    async def _reconnect_transport(self, failed_transport: NodeTransport) -> bool:
        raise NotImplementedError

    async def _read_loop(self) -> None:
        backoff = _RECONNECT_INITIAL_SECONDS
        while not self._closed:
            transport = self._transport
            try:
                message = await transport.receive()
                if message.kind in ("result", "error") and message.request_id:
                    future = self._pending.get(message.request_id)
                    if future is None or future.done():
                        continue
                    if message.kind == "error":
                        error = str((message.payload or {}).get("error", "node error"))
                        future.set_exception(ConnectionError(error))
                    else:
                        future.set_result(dict(message.payload or {}))
                elif (
                    message.kind in ("event", "health")
                    and self._event_handler is not None
                ):
                    queue = (
                        self._health_queue
                        if message.kind == "health"
                        else self._event_queue
                    )
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Remote node %s queue saturated; deferring delivery",
                            message.kind,
                        )
                backoff = _RECONNECT_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail_pending(ConnectionError(str(exc)))
                if self._closed or self._reconnect is None:
                    return
                logger.warning("Node relay reader disconnected: %s", exc)
                while not self._closed:
                    try:
                        if await self._reconnect_transport(transport):
                            logger.info("Node relay reader reconnected")
                            backoff = _RECONNECT_INITIAL_SECONDS
                            break
                    except asyncio.CancelledError:
                        raise
                    except Exception as reconnect_error:
                        logger.warning(
                            "Node relay reconnect failed; retrying in %.1fs: %s",
                            backoff,
                            reconnect_error,
                        )
                    await asyncio.sleep(backoff)
                    backoff = min(
                        _RECONNECT_MAX_SECONDS,
                        max(_RECONNECT_INITIAL_SECONDS, backoff * 2),
                    )

    async def _handle_events(self, queue: asyncio.Queue[NodeEnvelope]) -> None:
        while True:
            message = await queue.get()
            try:
                node_id = ""
                cursor_key = ""
                if message.kind == "event" and message.sequence:
                    payload = message.payload or {}
                    node_id = str(payload.get("node_id", ""))
                    stream_id = str(payload.get("event_stream_id", ""))
                    cursor_key = f"{node_id}\0{stream_id}" if stream_id else node_id
                    applied = self._event_cursors.last_applied(cursor_key)
                    if message.sequence <= applied:
                        await self._ack_event(message, node_id)
                        continue
                    if message.sequence != applied + 1:
                        logger.warning(
                            "Remote node event gap node=%s expected=%d got=%d",
                            node_id,
                            applied + 1,
                            message.sequence,
                        )
                        continue
                if self._event_handler is not None:
                    await self._event_handler(message)
                if message.kind == "event" and message.sequence:
                    self._event_cursors.commit(cursor_key, message.sequence)
                    await self._ack_event(message, node_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Remote node event handling failed")
            finally:
                queue.task_done()

    async def _ack_event(self, message: NodeEnvelope, node_id: str) -> None:
        await self._transport.send(
            NodeEnvelope(
                kind="ack",
                request_id=message.request_id,
                sequence=message.sequence,
                payload={"target_node_id": node_id},
            )
        )

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)


__all__ = ["NodeRpcEventMixin", "RpcEventHandler"]
