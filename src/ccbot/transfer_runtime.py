"""Runtime seam for starting a transferred session on a target node.

The leader owns the transfer state and Telegram UI. Concrete local/remote
node runtimes are registered by the execution layer; the UI never falls back
to a different node when a target runtime is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from .session_models import Session
from .transfer_models import SessionTransfer
from .transfer_queue import TransferDelivery


@dataclass
class TransferRuntimeResult:
    """Durable target facts returned after the target session is ready."""

    target_window_id: str = ""
    target_workdir: str = ""
    target_agent_session_id: str = ""
    target_context_path: str = ""
    context_error: str = ""
    delivery: TransferDelivery | None = None


class NodeRuntime(Protocol):
    async def list_directories(
        self, target_node_id: str, path: str = ""
    ) -> dict[str, Any]: ...

    async def create_directory(
        self, target_node_id: str, path: str, name: str
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def resolve_provider_session(
        self, target_node_id: str, path: str, backend: str, session_id: str
    ) -> dict[str, Any]: ...

    async def send_text(
        self, target_node_id: str, session_id: str, text: str
    ) -> dict[str, Any]: ...

    async def upload_inbox_file(
        self, target_node_id: str, session_id: str, filename: str, content: bytes
    ) -> dict[str, Any]: ...

    async def inspect_session(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]: ...

    async def reset_session_binding(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]: ...

    async def stat_session_file(
        self, target_node_id: str, session_id: str, path: str
    ) -> dict[str, Any]: ...

    async def download_session_file(
        self, target_node_id: str, session_id: str, path: str
    ) -> dict[str, Any]: ...

    async def download_session_file_to(
        self,
        target_node_id: str,
        session_id: str,
        path: str,
        destination: BinaryIO,
        *,
        expected_size: int,
        expected_version: str = "",
    ) -> dict[str, Any]: ...

    async def seed_session_history(
        self,
        target_node_id: str,
        session_id: str,
        max_turns: int,
        *,
        known_version: str = "",
    ) -> dict[str, Any]: ...

    async def send_key(
        self, target_node_id: str, session_id: str, key: str
    ) -> dict[str, Any]: ...

    async def capture_session(
        self,
        target_node_id: str,
        session_id: str,
        *,
        with_ansi: bool = False,
    ) -> dict[str, Any]: ...

    async def terminate_session(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]: ...

    async def revoke_node(self, target_node_id: str) -> dict[str, Any]: ...

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
