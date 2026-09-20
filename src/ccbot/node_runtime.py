"""Leader-side RPC and remote node runtime for session transfer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .node_transport import NodeEnvelope, NodeTransport, RequestReceiptLedger
from .node_transport import connect_relay
from .transfer_models import SessionTransfer
from .transfer_runtime import (
    TransferRuntimeResult,
    register_node_runtime,
    unregister_node_runtime,
)
from .session_models import Session

logger = logging.getLogger(__name__)

RpcEventHandler = Callable[[NodeEnvelope], Awaitable[None]]
TransportFactory = Callable[[], Awaitable[NodeTransport]]
_leader_rpc: NodeRpcClient | None = None
_remote_node_ids: set[str] = set()


class NodeRpcClient:
    """Multiplex request/result RPC over a relay connection.

    A retry keeps the same request ID. The worker-side receipt ledger can then
    return the original result after a disconnect without repeating a command.
    """

    def __init__(
        self,
        transport: NodeTransport,
        *,
        reconnect: TransportFactory | None = None,
        event_handler: RpcEventHandler | None = None,
        request_timeout: float = 60.0,
    ):
        self._transport = transport
        self._reconnect = reconnect
        self._event_handler = event_handler
        self._request_timeout = request_timeout
        self._ledger = RequestReceiptLedger()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="node-rpc-reader"
            )

    async def close(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._transport.close()
        self._fail_pending(ConnectionError("node RPC closed"))

    async def request(
        self,
        target_node_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        retries: int = 1,
    ) -> dict[str, Any]:
        if not target_node_id or not operation:
            raise ValueError("target node and operation are required")
        await self.start()
        request_id = self._ledger.new_request_id()
        body = dict(payload or {})
        body.update({"target_node_id": target_node_id, "operation": operation})
        attempts = max(0, retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._request_once(request_id, body)
            except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= attempts or self._reconnect is None:
                    raise
                await self._reconnect_transport()
        raise last_error or RuntimeError("node RPC request failed")

    async def _request_once(
        self, request_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._transport.send(
                NodeEnvelope(kind="command", request_id=request_id, payload=payload)
            )
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self._request_timeout
            )
        finally:
            self._pending.pop(request_id, None)

    async def _reconnect_transport(self) -> None:
        if self._reconnect is None:
            return
        async with self._reconnect_lock:
            if self._reader_task is not None and not self._reader_task.done():
                return
            try:
                await self._transport.close()
            except Exception:
                pass
            self._transport = await self._reconnect()
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="node-rpc-reader"
            )

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await self._transport.receive()
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
                    await self._event_handler(message)
                # Acks are deliberately not terminal: request completion is
                # proven only by the matching result.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(ConnectionError(str(exc)))

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)


class RemoteNodeRuntime:
    """Transfer runtime using typed RPC commands over the relay."""

    def __init__(self, rpc: Any, *, chunk_size: int = 256 * 1024):
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self._rpc = rpc
        self._chunk_size = chunk_size

    async def list_directories(
        self, target_node_id: str, path: str = ""
    ) -> dict[str, Any]:
        result = await self._request(target_node_id, "list_directories", {"path": path})
        self._require_ok(result)
        return result

    async def create_directory(
        self, target_node_id: str, path: str, name: str
    ) -> dict[str, Any]:
        result = await self._request(
            target_node_id,
            "create_directory",
            {"path": path, "name": name},
        )
        self._require_ok(result)
        return result

    async def create_session(
        self, target_node_id: str, path: str, backend: str, name: str
    ) -> dict[str, Any]:
        result = await self._request(
            target_node_id,
            "create_session",
            {"path": path, "backend": backend, "name": name},
        )
        self._require_ok(result)
        return result

    async def send_text(
        self, target_node_id: str, session_id: str, text: str
    ) -> dict[str, Any]:
        return await self._request(
            target_node_id,
            "send_text",
            {"session_id": session_id, "text": text},
        )

    async def start_context_transfer(
        self,
        *,
        transfer: SessionTransfer,
        source: Session,
        user_id: int,
        bot: Any,
    ) -> TransferRuntimeResult:
        del user_id, bot
        path = Path(transfer.context_path)
        content = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(content).hexdigest()
        begin = await self._request(
            transfer.target_node_id,
            "transfer_context_begin",
            {
                "transfer_id": transfer.id,
                "source_session_id": source.id,
                "source_name": source.name,
                "source_backend": source.backend,
                "target_backend": transfer.target_backend,
                "total_bytes": len(content),
                "sha256": digest,
            },
        )
        self._require_ok(begin)
        worker_transfer_id = str(begin.get("transfer_id", ""))
        if not worker_transfer_id:
            raise RuntimeError("worker did not return a context transfer id")
        for index, start in enumerate(range(0, len(content), self._chunk_size)):
            chunk = content[start : start + self._chunk_size]
            self._require_ok(
                await self._request(
                    transfer.target_node_id,
                    "transfer_context_chunk",
                    {
                        "transfer_id": worker_transfer_id,
                        "chunk_index": index,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
            )
        finished = await self._request(
            transfer.target_node_id,
            "transfer_context_finish",
            {"transfer_id": worker_transfer_id},
        )
        self._require_ok(finished)
        target_session_id = str(finished.get("target_agent_session_id", ""))

        async def deliver(update: Any, context: Any) -> bool:
            del context
            message = getattr(update, "message", None)
            text = getattr(message, "text", None) if message is not None else None
            if not text:
                return False
            result = await self.send_text(
                transfer.target_node_id, target_session_id, str(text)
            )
            return bool(result.get("ok", True))

        return TransferRuntimeResult(
            target_window_id=str(finished.get("target_window_id", "")),
            target_workdir=str(finished.get("target_workdir", "")),
            target_agent_session_id=target_session_id,
            target_context_path=str(finished.get("context_path", "")),
            context_error=str(finished.get("context_error", "")),
            delivery=deliver,
        )

    async def _request(
        self, target_node_id: str, operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rpc.request(target_node_id, operation, payload, retries=2)

    @staticmethod
    def _require_ok(result: dict[str, Any]) -> None:
        if result.get("ok", True) is False:
            raise RuntimeError(str(result.get("error", "worker command failed")))


async def connect_leader_rpc(
    *,
    host: str,
    port: int,
    leader_id: str,
    secret: str,
    ssl: Any = None,
    request_timeout: float = 60.0,
    event_handler: RpcEventHandler | None = None,
) -> NodeRpcClient:
    """Open the leader's single relay connection for all worker runtimes."""
    transport = await connect_relay(
        host,
        port,
        node_id=leader_id,
        role="leader",
        secret=secret,
        leader_id=leader_id,
        ssl=ssl,
    )

    async def reconnect() -> NodeTransport:
        return await connect_relay(
            host,
            port,
            node_id=leader_id,
            role="leader",
            secret=secret,
            leader_id=leader_id,
            ssl=ssl,
        )

    client = NodeRpcClient(
        transport,
        reconnect=reconnect,
        event_handler=event_handler,
        request_timeout=request_timeout,
    )
    await client.start()
    return client


def _relay_endpoint(raw_url: str, tls: bool) -> tuple[str, int, Any]:
    parsed = urlparse(raw_url if "://" in raw_url else f"tcp://{raw_url}")
    host = parsed.hostname
    port = parsed.port
    use_tls = tls or parsed.scheme in ("tls", "ssl", "https")
    if not host or port is None:
        raise ValueError("relay URL must include host and port")
    context = ssl.create_default_context() if use_tls else None
    return host, port, context


async def connect_configured_remote_runtimes(
    *,
    relay_url: str,
    leader_id: str,
    secret: str,
    node_ids: list[str],
    tls: bool = False,
) -> NodeRpcClient:
    """Connect one leader RPC and register runtimes for known remote nodes."""
    global _leader_rpc
    if _leader_rpc is not None:
        return _leader_rpc
    host, port, ssl_context = _relay_endpoint(relay_url, tls)

    async def handle_node_event(message: NodeEnvelope) -> None:
        payload = message.payload or {}
        node_id = str(payload.get("node_id", ""))
        if not node_id or node_id == "local":
            return
        from .node_models import Node
        from .session import session_manager

        state = payload.get("state", "online")
        if state not in ("online", "ready", "offline", "pending"):
            state = "online"
        existing = session_manager.get_node(node_id)
        node = existing or Node(node_id, node_id)
        node.display_name = str(payload.get("display_name", node.display_name))
        node.state = state
        node.platform = str(payload.get("platform", node.platform))
        node.arch = str(payload.get("arch", node.arch))
        node.backends = [
            str(value)
            for value in payload.get("backends", [])
            if value in ("claude", "codex")
        ]
        node.capabilities = {
            str(key): bool(value)
            for key, value in (payload.get("capabilities", {}) or {}).items()
        }
        node.last_seen_at = time.time()
        session_manager.register_node(node)
        if _leader_rpc is not None:
            register_remote_runtime(node_id)

    _leader_rpc = await connect_leader_rpc(
        host=host,
        port=port,
        leader_id=leader_id,
        secret=secret,
        ssl=ssl_context,
        event_handler=handle_node_event,
    )
    for node_id in node_ids:
        if node_id == "local":
            continue
        register_node_runtime(node_id, RemoteNodeRuntime(_leader_rpc))
        _remote_node_ids.add(node_id)
    return _leader_rpc


def register_remote_runtime(node_id: str) -> None:
    if _leader_rpc is None:
        raise RuntimeError("leader relay RPC is not connected")
    register_node_runtime(node_id, RemoteNodeRuntime(_leader_rpc))
    _remote_node_ids.add(node_id)


async def shutdown_remote_runtimes() -> None:
    global _leader_rpc
    for node_id in tuple(_remote_node_ids):
        unregister_node_runtime(node_id)
    _remote_node_ids.clear()
    if _leader_rpc is not None:
        await _leader_rpc.close()
        _leader_rpc = None


__all__ = [
    "NodeRpcClient",
    "RemoteNodeRuntime",
    "connect_configured_remote_runtimes",
    "connect_leader_rpc",
    "register_remote_runtime",
    "shutdown_remote_runtimes",
]
