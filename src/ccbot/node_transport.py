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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from .node_pairing import pairing_signature


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
        self.reconnect_secret = ""

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

    def __init__(
        self,
        *,
        credentials: dict[str, str],
        leader_id: str,
        revocations_path: str | Path | None = None,
    ):
        self._credentials = dict(credentials)
        self.leader_id = leader_id
        self._server: asyncio.AbstractServer | None = None
        self._connections: dict[str, tuple[str, StreamNodeTransport]] = {}
        self._consumed_pairing_nonces: set[str] = set()
        self._revocations_path = (
            Path(revocations_path).expanduser() if revocations_path else None
        )
        self._revoked_node_ids = self._load_revocations()

    def _load_revocations(self) -> set[str]:
        if self._revocations_path is None:
            return set()
        try:
            values = json.loads(self._revocations_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        return (
            {str(value) for value in values if str(value)}
            if isinstance(values, list)
            else set()
        )

    def _save_revocations(self) -> None:
        if self._revocations_path is None:
            return
        path = self._revocations_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(sorted(self._revoked_node_ids)), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    @property
    def port(self) -> int:
        sockets = getattr(self._server, "sockets", None)
        if not sockets:
            raise RuntimeError("relay server is not started")
        return int(sockets[0].getsockname()[1])

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
            node_id, role, paired = self._authenticate(handshake)
            previous = self._connections.get(node_id)
            self._connections[node_id] = (role, transport)
            if previous is not None:
                await previous[1].close()
            await transport.send(
                NodeEnvelope(
                    kind="handshake",
                    payload={
                        "ok": True,
                        "leader_id": self.leader_id,
                        "reconnect_secret": (
                            self._worker_reconnect_secret(node_id) if paired else ""
                        ),
                    },
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

    def _authenticate(self, message: NodeEnvelope) -> tuple[str, str, bool]:
        if message.kind != "handshake":
            raise PermissionError("relay authentication failed")
        payload = message.payload or {}
        node_id = str(payload.get("node_id", ""))
        role = str(payload.get("role", ""))
        secret = str(payload.get("secret", ""))
        claimed_leader = str(payload.get("leader_id", ""))
        expected = self._credentials.get(node_id, "")
        if role == "worker" and node_id in self._revoked_node_ids:
            raise PermissionError("relay authentication failed")
        derived_worker_secret = (
            self._worker_reconnect_secret(node_id) if role == "worker" else ""
        )
        regular_credentials = (
            bool(expected) and hmac.compare_digest(secret, expected)
        ) or (
            bool(derived_worker_secret)
            and hmac.compare_digest(secret, derived_worker_secret)
        )
        pairing_credentials = self._pairing_credentials_match(payload, secret)
        if (
            not node_id
            or role not in ("leader", "worker")
            or claimed_leader != self.leader_id
            or (role == "leader" and node_id != self.leader_id)
            or (role == "worker" and node_id == self.leader_id)
            or not (regular_credentials or pairing_credentials)
        ):
            raise PermissionError("relay authentication failed")
        if pairing_credentials:
            self._consumed_pairing_nonces.add(str(payload.get("pairing_nonce", "")))
        return node_id, role, pairing_credentials

    def _worker_reconnect_secret(self, node_id: str) -> str:
        leader_secret = self._credentials.get(self.leader_id, "")
        if not leader_secret or not node_id:
            return ""
        payload = f"ccbot-worker-reconnect\0{self.leader_id}\0{node_id}".encode()
        return hmac.new(leader_secret.encode(), payload, "sha256").hexdigest()

    def _pairing_credentials_match(self, payload: dict[str, Any], secret: str) -> bool:
        """Accept an unknown worker with a leader-signed bootstrap token."""
        if str(payload.get("role", "")) != "worker":
            return False
        nonce = str(payload.get("pairing_nonce", ""))
        node_id = str(payload.get("node_id", ""))
        try:
            expires_at = float(payload.get("pairing_expires", 0))
        except (TypeError, ValueError):
            return False
        if (
            not nonce
            or not node_id
            or nonce in self._consumed_pairing_nonces
            or expires_at <= time.time()
        ):
            return False
        leader_secret = self._credentials.get(self.leader_id, "")
        if not leader_secret:
            return False
        expected = pairing_signature(
            leader_secret,
            leader_id=self.leader_id,
            node_id=node_id,
            nonce=nonce,
            expires_at=expires_at,
        )
        return hmac.compare_digest(secret, expected)

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
            if payload.get("operation") == "revoke_node":
                self._revoked_node_ids.add(target_id)
                self._save_revocations()
                await sender.send(
                    NodeEnvelope(
                        kind="result",
                        request_id=message.request_id,
                        payload={"ok": True, "revoked_node_id": target_id},
                    )
                )
                target = self._connections.get(target_id)
                if target is not None:
                    await target[1].close()
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
    pairing_nonce: str = "",
    pairing_expires: float = 0.0,
) -> StreamNodeTransport:
    """Connect and authenticate one leader/worker relay participant."""
    transport = await StreamNodeTransport.connect(host, port, ssl=ssl)
    payload: dict[str, Any] = {
        "node_id": node_id,
        "role": role,
        "secret": secret,
        "leader_id": leader_id,
    }
    if pairing_nonce:
        payload["pairing_nonce"] = pairing_nonce
        payload["pairing_expires"] = pairing_expires
    await transport.send(NodeEnvelope(kind="handshake", payload=payload))
    response = await transport.receive()
    if response.kind == "error":
        await transport.close()
        raise PermissionError(
            str((response.payload or {}).get("error", "relay authentication failed"))
        )
    if response.kind != "handshake" or not (response.payload or {}).get("ok"):
        await transport.close()
        raise PermissionError("relay authentication failed")
    transport.reconnect_secret = str(
        (response.payload or {}).get("reconnect_secret", "")
    )
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
