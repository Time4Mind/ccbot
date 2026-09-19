"""Control-plane message contract for relay-backed node transport.

The relay is a rendezvous/data path with a stable public address. It does not
own Telegram state or provider sessions. Concrete socket/HTTP adapters can
implement ``NodeTransport`` without changing the idempotency and event-order
rules enforced by the leader.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol, Self


_KINDS = {"handshake", "command", "ack", "result", "event", "health", "error"}


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
    "RequestReceipt",
    "RequestReceiptLedger",
]
