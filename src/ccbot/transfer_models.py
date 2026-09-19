"""Persisted state for a session-context transfer."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Literal


TransferState = Literal["pending", "starting", "ready", "failed", "cancelled"]
_TRANSFER_STATES = {"pending", "starting", "ready", "failed", "cancelled"}


@dataclass
class SessionTransfer:
    """Leader-side lifecycle record for one context import."""

    id: str
    source_session_id: str
    source_node_id: str
    target_node_id: str
    target_backend: str
    target_session_id: str = ""
    context_path: str = ""
    state: TransferState = "pending"
    error: str = ""
    queued_requests: list[str] = field(default_factory=list)
    created_at: float = 0.0

    @staticmethod
    def new_id() -> str:
        return secrets.token_urlsafe(12)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_session_id": self.source_session_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "target_backend": self.target_backend,
            "target_session_id": self.target_session_id,
            "context_path": self.context_path,
            "state": self.state,
            "error": self.error,
            "queued_requests": list(self.queued_requests),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionTransfer":
        state = data.get("state", "pending")
        if state not in _TRANSFER_STATES:
            state = "failed"
        queued = data.get("queued_requests", [])
        return cls(
            id=str(data.get("id", "")),
            source_session_id=str(data.get("source_session_id", "")),
            source_node_id=str(data.get("source_node_id", "local")),
            target_node_id=str(data.get("target_node_id", "")),
            target_backend=str(data.get("target_backend", "")),
            target_session_id=str(data.get("target_session_id", "")),
            context_path=str(data.get("context_path", "")),
            state=state,
            error=str(data.get("error", "")),
            queued_requests=[str(value) for value in queued if isinstance(value, str)]
            if isinstance(queued, list)
            else [],
            created_at=float(data.get("created_at", 0.0)),
        )


__all__ = ["SessionTransfer", "TransferState"]
