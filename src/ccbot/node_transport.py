"""Control-plane message contract for relay-backed node transport.

The relay is a rendezvous/data path with a stable public address. It does not
own Telegram state or provider sessions. Concrete socket/HTTP adapters can
implement ``NodeTransport`` without changing the idempotency and event-order
rules enforced by the leader.
"""

from __future__ import annotations

import json
import asyncio
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Protocol, Self


_KINDS = {"handshake", "command", "ack", "result", "event", "health", "error"}
_MAX_LINE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class NodeEnvelope:
    kind: str
    request_id: str = ""
    sequence: int = 0
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported node message kind: {self.kind}")
        if self.sequence < 0:
            raise ValueError("node message sequence cannot be negative")
        if self.payload is not None and not isinstance(self.payload, dict):
            raise TypeError("node message payload must be an object")

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "request_id": self.request_id,
                "sequence": self.sequence,
                "payload": self.payload or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json_line(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid node message JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("node message must be a JSON object")
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("node message payload must be an object")
        try:
            sequence = int(data.get("sequence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("node message sequence must be an integer") from exc
        return cls(
            kind=str(data.get("kind", "")),
            request_id=str(data.get("request_id", "")),
            sequence=sequence,
            payload=payload,
        )


class NodeTransport(Protocol):
    """Minimal async seam implemented by a relay-backed adapter."""

    async def send(self, message: NodeEnvelope) -> None: ...

    async def receive(self) -> NodeEnvelope: ...

    async def close(self) -> None: ...


class StreamNodeTransport:
    """Newline-delimited JSON transport over one TCP connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        ssl: Any = None,
        connect_timeout: float = 10.0,
    ) -> "StreamNodeTransport":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl, limit=_MAX_LINE_BYTES),
            timeout=connect_timeout,
        )
        return cls(reader, writer)

    async def send(self, message: NodeEnvelope) -> None:
        data = (message.to_json_line() + "\n").encode("utf-8")
        if len(data) > _MAX_LINE_BYTES:
            raise ValueError("node message exceeds relay frame limit")
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def receive(self) -> NodeEnvelope:
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("node transport closed")
        if len(line) > _MAX_LINE_BYTES:
            raise ValueError("node message exceeds relay frame limit")
        return NodeEnvelope.from_json_line(line.decode("utf-8"))

    async def close(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass


class RelayServer:
    """Authenticated rendezvous relay for one leader and many workers.

    Both sides make an outbound connection to this server, so neither worker
    nor leader needs an externally reachable address. The relay only forwards
    envelopes; it does not persist Telegram state or provider context.
    """

    def __init__(self, *, credentials: dict[str, str], leader_id: str):
        self._credentials = dict(credentials)
        self.leader_id = leader_id
        self._server: asyncio.AbstractServer | None = None
        self._connections: dict[str, tuple[str, StreamNodeTransport]] = {}

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("relay server is not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self, host: str, port: int, *, ssl: Any = None) -> None:
        if self._server is not None:
            raise RuntimeError("relay server is already started")
        self._server = await asyncio.start_server(
            self._handle_client,
            host,
            port,
            ssl=ssl,
            limit=_MAX_LINE_BYTES,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("relay server is not started")
        await self._server.serve_forever()

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        connections = list(self._connections.values())
        self._connections.clear()
        await asyncio.gather(
            *(transport.close() for _role, transport in connections),
            return_exceptions=True,
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        transport = StreamNodeTransport(reader, writer)
        node_id = ""
        try:
            handshake = await asyncio.wait_for(transport.receive(), timeout=10.0)
            node_id, role = self._authenticate(handshake)
            previous = self._connections.get(node_id)
            self._connections[node_id] = (role, transport)
            if previous is not None:
                await previous[1].close()
            await transport.send(
                NodeEnvelope(
                    kind="handshake",
                    payload={"ok": True, "leader_id": self.leader_id},
                )
            )
            while True:
                await self._route(node_id, role, await transport.receive(), transport)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except (PermissionError, ValueError, asyncio.TimeoutError) as exc:
            try:
                await transport.send(
                    NodeEnvelope(kind="error", payload={"error": str(exc)})
                )
            except Exception:
                pass
        finally:
            current = self._connections.get(node_id)
            if current is not None and current[1] is transport:
                self._connections.pop(node_id, None)
            await transport.close()

    def _authenticate(self, message: NodeEnvelope) -> tuple[str, str]:
        if message.kind != "handshake":
            raise PermissionError("relay authentication failed")
        payload = message.payload or {}
        node_id = str(payload.get("node_id", ""))
        role = str(payload.get("role", ""))
        secret = str(payload.get("secret", ""))
        claimed_leader = str(payload.get("leader_id", ""))
        expected = self._credentials.get(node_id, "")
        if (
            not node_id
            or role not in ("leader", "worker")
            or not expected
            or not hmac.compare_digest(secret, expected)
            or claimed_leader != self.leader_id
            or (role == "leader" and node_id != self.leader_id)
            or (role == "worker" and node_id == self.leader_id)
        ):
            raise PermissionError("relay authentication failed")
        return node_id, role

    async def _route(
        self,
        sender_id: str,
        sender_role: str,
        message: NodeEnvelope,
        sender: StreamNodeTransport,
    ) -> None:
        payload = dict(message.payload or {})
        if sender_role == "leader":
            target_id = str(payload.get("target_node_id", ""))
            if not target_id:
                await sender.send(
                    NodeEnvelope(
                        kind="error",
                        request_id=message.request_id,
                        payload={"error": "target_node_id is required"},
                    )
                )
                return
        else:
            target_id = self.leader_id
        target = self._connections.get(target_id)
        if target is None:
            await sender.send(
                NodeEnvelope(
                    kind="error",
                    request_id=message.request_id,
                    payload={"error": f"target node is offline: {target_id}"},
                )
            )
            return
        await target[1].send(message)


async def connect_relay(
    host: str,
    port: int,
    *,
    node_id: str,
    role: str,
    secret: str,
    leader_id: str,
    ssl: Any = None,
) -> StreamNodeTransport:
    """Connect and authenticate one leader/worker relay participant."""
    transport = await StreamNodeTransport.connect(host, port, ssl=ssl)
    await transport.send(
        NodeEnvelope(
            kind="handshake",
            payload={
                "node_id": node_id,
                "role": role,
                "secret": secret,
                "leader_id": leader_id,
            },
        )
    )
    response = await transport.receive()
    if response.kind == "error":
        await transport.close()
        raise PermissionError(
            str((response.payload or {}).get("error", "relay authentication failed"))
        )
    if response.kind != "handshake" or not (response.payload or {}).get("ok"):
        await transport.close()
        raise PermissionError("relay authentication failed")
    return transport


@dataclass(frozen=True)
class RequestReceipt:
    request_id: str
    accepted: bool
    result: dict[str, Any] | None = None


class RequestReceiptLedger:
    """Make command retries idempotent across relay reconnects."""

    def __init__(self) -> None:
        self._receipts: dict[str, RequestReceipt] = {}

    def new_request_id(self) -> str:
        return secrets.token_urlsafe(12)

    def accept(self, request_id: str) -> RequestReceipt:
        if not request_id:
            raise ValueError("request id cannot be empty")
        existing = self._receipts.get(request_id)
        if existing is not None:
            return existing
        receipt = RequestReceipt(request_id=request_id, accepted=True)
        self._receipts[request_id] = receipt
        return receipt

    def complete(self, request_id: str, result: dict[str, Any]) -> RequestReceipt:
        receipt = self._receipts.get(request_id)
        if receipt is None:
            raise KeyError(f"unknown request id: {request_id}")
        completed = RequestReceipt(
            request_id=request_id,
            accepted=receipt.accepted,
            result=dict(result),
        )
        self._receipts[request_id] = completed
        return completed


class EventSequence:
    """Track the next event sequence expected from one node."""

    def __init__(self, next_sequence: int = 1) -> None:
        if next_sequence < 1:
            raise ValueError("event sequence starts at one")
        self.next_sequence = next_sequence

    def accept(self, message: NodeEnvelope) -> bool:
        if message.kind != "event":
            raise ValueError("event cursor accepts event messages only")
        if message.sequence < self.next_sequence:
            # Replayed event: already applied, safe to ignore.
            return False
        if message.sequence > self.next_sequence:
            raise ValueError(
                f"event gap: expected {self.next_sequence}, got {message.sequence}"
            )
        self.next_sequence += 1
        return True


__all__ = [
    "EventSequence",
    "NodeEnvelope",
    "NodeTransport",
    "RelayServer",
    "RequestReceipt",
    "RequestReceiptLedger",
    "StreamNodeTransport",
    "connect_relay",
]
