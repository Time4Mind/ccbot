"""Leader-side RPC and remote node runtime for session transfer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .node_transport import NodeEnvelope, NodeTransport, RequestReceiptLedger
from .node_transport import connect_relay
from .node_update import current_git_revision
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
_remote_message_handler: Callable[[Any], Awaitable[None]] | None = None
_node_update_tasks: dict[str, asyncio.Task[None]] = {}
_node_update_attempts: dict[str, float] = {}
_NODE_UPDATE_RETRY_SECONDS = 300.0
_RECONNECT_INITIAL_SECONDS = 0.5
_RECONNECT_MAX_SECONDS = 5.0


async def _request_node_update(node_id: str, revision: str) -> None:
    if _leader_rpc is None:
        return
    try:
        result = await _leader_rpc.request(
            node_id,
            "update_runtime",
            {"revision": revision},
            retries=0,
            timeout=600.0,
        )
        if not result.get("ok"):
            logger.warning(
                "node auto-update rejected node=%s revision=%s error=%s",
                node_id,
                revision,
                result.get("error", "unknown error"),
            )
    except Exception as exc:
        logger.warning(
            "node auto-update failed node=%s revision=%s error=%s",
            node_id,
            revision,
            exc,
        )


def _schedule_node_update(node_id: str, revision: str) -> None:
    now = time.monotonic()
    active = _node_update_tasks.get(node_id)
    if active is not None and not active.done():
        return
    last_attempt = _node_update_attempts.get(node_id)
    if last_attempt is not None and now - last_attempt < _NODE_UPDATE_RETRY_SECONDS:
        return
    _node_update_attempts[node_id] = now
    task = asyncio.create_task(
        _request_node_update(node_id, revision),
        name=f"node-update-{node_id}",
    )
    _node_update_tasks[node_id] = task

    def discard(completed: asyncio.Task[None]) -> None:
        if _node_update_tasks.get(node_id) is completed:
            _node_update_tasks.pop(node_id, None)

    task.add_done_callback(discard)


def set_remote_message_handler(
    handler: Callable[[Any], Awaitable[None]] | None,
) -> None:
    global _remote_message_handler
    _remote_message_handler = handler


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
        self._event_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[NodeEnvelope] = asyncio.Queue(maxsize=1024)
        self._health_queue: asyncio.Queue[NodeEnvelope] = asyncio.Queue(maxsize=256)
        self._reconnect_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise ConnectionError("node RPC is closed")
        if self._event_handler is not None:
            if self._event_task is None or self._event_task.done():
                self._event_task = asyncio.create_task(
                    self._handle_events(self._event_queue), name="node-rpc-events"
                )
            if self._health_task is None or self._health_task.done():
                self._health_task = asyncio.create_task(
                    self._handle_events(self._health_queue), name="node-rpc-health"
                )
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="node-rpc-reader"
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task, self._reader_task = self._reader_task, None
        background = [task, self._event_task, self._health_task]
        self._event_task = None
        self._health_task = None
        for background_task in background:
            if background_task is not None and not background_task.done():
                background_task.cancel()
        await asyncio.gather(
            *(item for item in background if item is not None), return_exceptions=True
        )
        await self._transport.close()
        self._fail_pending(ConnectionError("node RPC closed"))

    async def request(
        self,
        target_node_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        retries: int = 1,
        timeout: float | None = None,
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
            transport = self._transport
            try:
                return await self._request_once(
                    request_id, body, transport=transport, timeout=timeout
                )
            except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= attempts or self._reconnect is None:
                    raise
                await self._reconnect_transport(transport)
        raise last_error or RuntimeError("node RPC request failed")

    async def _request_once(
        self,
        request_id: str,
        payload: dict[str, Any],
        *,
        transport: NodeTransport,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await transport.send(
                NodeEnvelope(kind="command", request_id=request_id, payload=payload)
            )
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._request_timeout if timeout is None else timeout,
            )
        finally:
            self._pending.pop(request_id, None)

    async def _reconnect_transport(self, failed_transport: NodeTransport) -> bool:
        if self._reconnect is None or self._closed:
            return False
        async with self._reconnect_lock:
            if self._closed:
                return False
            if self._transport is not failed_transport:
                return True
            try:
                await failed_transport.close()
            except Exception:
                pass
            replacement = await self._reconnect()
            if self._closed:
                await replacement.close()
                return False
            self._transport = replacement
            return True

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
                    await queue.put(message)
                # Acks are deliberately not terminal: request completion is
                # proven only by the matching result.
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
                if self._event_handler is not None:
                    await self._event_handler(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Remote node event handling failed")
            finally:
                queue.task_done()

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
        self,
        target_node_id: str,
        path: str,
        backend: str,
        name: str,
        *,
        resume_session_id: str = "",
        source_backend: str = "",
        provider_transcript_path: str = "",
    ) -> dict[str, Any]:
        startup_id = secrets.token_urlsafe(12)
        payload = {
            "path": path,
            "backend": backend,
            "name": name,
            "startup_id": startup_id,
        }
        if resume_session_id:
            payload["resume_session_id"] = resume_session_id
        if source_backend:
            payload["source_backend"] = source_backend
        if provider_transcript_path:
            payload["provider_transcript_path"] = provider_transcript_path
        try:
            result = await self._request(
                target_node_id,
                "restore_session" if resume_session_id else "create_session",
                payload,
            )
        except BaseException:
            try:
                await asyncio.shield(
                    self._request(
                        target_node_id,
                        "cancel_session_start",
                        {"startup_id": startup_id},
                    )
                )
            except BaseException as cleanup_error:
                logger.warning(
                    "Could not cancel remote session startup node=%s startup=%s: %s",
                    target_node_id,
                    startup_id,
                    cleanup_error,
                )
            raise
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

    async def resolve_provider_session(
        self, target_node_id: str, path: str, backend: str, session_id: str
    ) -> dict[str, Any]:
        return await self._request(
            target_node_id,
            "resolve_provider_session",
            {"path": path, "backend": backend, "session_id": session_id},
        )

    async def send_key(
        self, target_node_id: str, session_id: str, key: str
    ) -> dict[str, Any]:
        return await self._request(
            target_node_id, "send_key", {"session_id": session_id, "key": key}
        )

    async def capture_session(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]:
        return await self._request(
            target_node_id, "capture_session", {"session_id": session_id}
        )

    async def terminate_session(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]:
        return await self._request(
            target_node_id, "terminate_session", {"session_id": session_id}
        )

    async def revoke_node(self, target_node_id: str) -> dict[str, Any]:
        return await self._request(target_node_id, "revoke_node", {})

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
        if operation in {
            "send_key",
            "capture_session",
            "terminate_session",
            "cancel_session_start",
            "revoke_node",
        }:
            return await self._rpc.request(
                target_node_id,
                operation,
                payload,
                retries=0,
                timeout=5.0,
            )
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
    leader_revision = current_git_revision()
    if not leader_revision:
        logger.warning(
            "node auto-update disabled: leader is not running a clean Git revision"
        )
    host, port, ssl_context = _relay_endpoint(relay_url, tls)

    async def handle_node_event(message: NodeEnvelope) -> None:
        payload = message.payload or {}
        node_id = str(payload.get("node_id", ""))
        if not node_id or node_id == "local":
            return
        from .node_models import Node
        from .session import session_manager

        if node_id in session_manager.removed_node_ids:
            return
        if message.kind == "event" and payload.get("event_type") == "session_binding":
            bound = session_manager.bind_remote_session(
                node_id,
                str(payload.get("session_id", "")),
                str(payload.get("provider_session_id", "")),
                str(payload.get("transcript_path", "")),
            )
            if bound is None:
                logger.warning(
                    "remote session binding has no owner node=%s routing=%s",
                    node_id,
                    payload.get("session_id", ""),
                )
            return
        if message.kind == "event" and payload.get("event_type") == "session_message":
            if _remote_message_handler is not None:
                from .session_monitor import NewMessage

                await _remote_message_handler(
                    NewMessage(
                        session_id=str(payload.get("session_id", "")),
                        text=str(payload.get("text", "")),
                        is_complete=bool(payload.get("stop_reason")),
                        content_type=str(payload.get("content_type", "text")),
                        tool_use_id=payload.get("tool_use_id") or None,
                        role=str(payload.get("role", "assistant")),
                        tool_name=payload.get("tool_name") or None,
                        stop_reason=payload.get("stop_reason") or None,
                        timestamp=str(payload.get("timestamp", "")),
                        is_error=bool(payload.get("is_error", False)),
                        api_error=str(payload.get("api_error", "")),
                    )
                )
            return

        state = payload.get("state", "online")
        if state not in ("online", "ready", "offline", "pending"):
            state = "online"
        existing = session_manager.get_node(node_id)
        durable_before = (
            None
            if existing is None
            else (
                existing.display_name,
                existing.state,
                existing.platform,
                existing.arch,
                tuple(existing.backends),
                tuple(sorted(existing.capabilities.items())),
                existing.ccbot_version,
                existing.ssh_host,
                existing.ssh_user,
                existing.ssh_port,
                existing.ssh_proxy_jump,
            )
        )
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
        node.capacity = {
            str(key): max(0, int(value))
            for key, value in (payload.get("capacity", {}) or {}).items()
            if isinstance(value, (int, float))
        }
        node.ccbot_version = str(payload.get("ccbot_version", node.ccbot_version))
        raw_ssh = payload.get("ssh")
        if isinstance(raw_ssh, dict):
            node.ssh_host = str(raw_ssh.get("host", ""))
            node.ssh_user = str(raw_ssh.get("user", ""))
            raw_port = raw_ssh.get("port", 22)
            node.ssh_port = (
                int(raw_port)
                if isinstance(raw_port, (int, float, str)) and str(raw_port).isdigit()
                else 22
            )
            node.ssh_proxy_jump = str(raw_ssh.get("proxy_jump", ""))
        node.last_seen_at = time.time()
        durable_after = (
            node.display_name,
            node.state,
            node.platform,
            node.arch,
            tuple(node.backends),
            tuple(sorted(node.capabilities.items())),
            node.ccbot_version,
            node.ssh_host,
            node.ssh_user,
            node.ssh_port,
            node.ssh_proxy_jump,
        )
        session_manager.register_node(node, persist=durable_before != durable_after)
        if _leader_rpc is not None and node.enabled and node_id not in _remote_node_ids:
            register_remote_runtime(node_id)
            _remote_node_ids.add(node_id)
        if (
            message.kind == "health"
            and leader_revision
            and node.ccbot_version
            and node.ccbot_version != leader_revision
            and node.enabled
        ):
            _schedule_node_update(node_id, leader_revision)

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
    update_tasks = list(_node_update_tasks.values())
    _node_update_tasks.clear()
    _node_update_attempts.clear()
    for task in update_tasks:
        task.cancel()
    await asyncio.gather(*update_tasks, return_exceptions=True)
    if _leader_rpc is not None:
        await _leader_rpc.close()
        _leader_rpc = None
    set_remote_message_handler(None)


__all__ = [
    "NodeRpcClient",
    "RemoteNodeRuntime",
    "connect_configured_remote_runtimes",
    "connect_leader_rpc",
    "register_remote_runtime",
    "set_remote_message_handler",
    "shutdown_remote_runtimes",
]
