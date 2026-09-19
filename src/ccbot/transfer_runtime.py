"""Runtime seam for starting a transferred session on a target node.

The leader owns the transfer state and Telegram UI. Concrete local/remote
node runtimes are registered by the execution layer; the UI never falls back
to a different node when a target runtime is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .session_models import Session
from .transfer_models import SessionTransfer
from .transfer_queue import TransferDelivery


@dataclass
class TransferRuntimeResult:
    """Durable target facts returned after the target session is ready."""

    target_window_id: str = ""
    target_workdir: str = ""
    target_agent_session_id: str = ""
    context_error: str = ""
    delivery: TransferDelivery | None = None


class NodeRuntime(Protocol):
    async def start_context_transfer(
        self,
        *,
        transfer: SessionTransfer,
        source: Session,
        user_id: int,
        bot: Any,
    ) -> TransferRuntimeResult: ...


_runtimes: dict[str, NodeRuntime] = {}


def register_node_runtime(node_id: str, runtime: NodeRuntime) -> None:
    if not node_id:
        raise ValueError("node_id cannot be empty")
    _runtimes[node_id] = runtime


def unregister_node_runtime(node_id: str) -> None:
    _runtimes.pop(node_id, None)


def get_node_runtime(node_id: str) -> NodeRuntime | None:
    return _runtimes.get(node_id)


def reset_node_runtimes_for_test() -> None:
    _runtimes.clear()


__all__ = [
    "NodeRuntime",
    "TransferRuntimeResult",
    "get_node_runtime",
    "register_node_runtime",
    "reset_node_runtimes_for_test",
    "unregister_node_runtime",
]
