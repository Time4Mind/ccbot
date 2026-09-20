"""Persisted node identity and runtime capability metadata.

The leader owns this registry.  A node record deliberately contains only
descriptive and health data; pairing credentials and transport secrets never
belong in the state file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Literal


NodeState = Literal["pending", "online", "offline", "ready"]
_NODE_STATES = {"pending", "online", "offline", "ready"}
NODE_HEALTH_STALE_SECONDS = 45.0


@dataclass
class Node:
    """A registered execution node known to the leader."""

    id: str
    display_name: str
    state: NodeState = "offline"
    platform: str = ""
    arch: str = ""
    backends: list[str] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    capacity: dict[str, int] = field(default_factory=dict)
    protocol_version: str = ""
    ccbot_version: str = ""
    last_seen_at: float = 0.0
    health_reason: str = ""
    enabled: bool = True

    def is_available(self, *, now: float | None = None) -> bool:
        """Return whether this node is currently safe to admit new work."""
        if self.id == "local":
            return True
        if not self.enabled:
            return False
        if self.state != "ready":
            return False
        if self.last_seen_at <= 0:
            return True
        current = time.time() if now is None else now
        return current - self.last_seen_at < NODE_HEALTH_STALE_SECONDS

    @classmethod
    def local(cls) -> "Node":
        """Return the implicit node used by pre-multi-node state."""
        return cls(
            id="local",
            display_name="local",
            state="ready",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "state": self.state,
            "platform": self.platform,
            "arch": self.arch,
            "backends": list(self.backends),
            "capabilities": dict(self.capabilities),
            "capacity": dict(self.capacity),
            "protocol_version": self.protocol_version,
            "ccbot_version": self.ccbot_version,
            "last_seen_at": self.last_seen_at,
            "health_reason": self.health_reason,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Node":
        state = data.get("state", "offline")
        if state not in _NODE_STATES:
            state = "offline"
        raw_backends = data.get("backends", [])
        backends = (
            list(dict.fromkeys(str(value) for value in raw_backends))
            if isinstance(raw_backends, list)
            else []
        )
        raw_capabilities = data.get("capabilities", {})
        capabilities = (
            {str(key): bool(value) for key, value in raw_capabilities.items()}
            if isinstance(raw_capabilities, dict)
            else {}
        )
        raw_capacity = data.get("capacity", {})
        capacity = (
            {
                str(key): max(0, int(value))
                for key, value in raw_capacity.items()
                if isinstance(value, (int, float))
            }
            if isinstance(raw_capacity, dict)
            else {}
        )
        return cls(
            id=str(data.get("id", "")),
            display_name=str(data.get("display_name", "")),
            state=state,
            platform=str(data.get("platform", "")),
            arch=str(data.get("arch", "")),
            backends=backends,
            capabilities=capabilities,
            capacity=capacity,
            protocol_version=str(data.get("protocol_version", "")),
            ccbot_version=str(data.get("ccbot_version", "")),
            last_seen_at=float(data.get("last_seen_at", 0.0)),
            health_reason=str(data.get("health_reason", "")),
            enabled=bool(data.get("enabled", True)),
        )


__all__ = ["NODE_HEALTH_STALE_SECONDS", "Node", "NodeState"]
