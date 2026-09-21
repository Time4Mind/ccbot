"""Node-scoped session selection and context-transfer state operations."""

from __future__ import annotations

import time
from typing import Any

from .node_models import Node
from .session_models import Session
from .transfer_models import SessionTransfer


class NodeSessionStateMixin:
    """Persisted node registry and leader-side transfer lifecycle."""

    nodes: dict[str, Node]
    removed_node_ids: set[str]
    selected_node_ids: dict[int, str]
    sessions: dict[str, Session]
    transfers: dict[str, SessionTransfer]
    active_sessions: dict[int, str]
    active_sessions_by_node: dict[int, dict[str, str]]
    save_state: Any
    create_session: Any
    set_active_session: Any
    mark_session_archived: Any
    get_enabled_backends: Any

    @property
    def registered_node_count(self) -> int:
        """Number of registered nodes, including the implicit local node."""
        return len(self.nodes)

    @property
    def has_multiple_nodes(self) -> bool:
        return self.registered_node_count > 1

    def list_nodes(self) -> list[Node]:
        """Return nodes in stable UI order: local first, then by name."""
        return sorted(
            self.nodes.values(),
            key=lambda node: (
                node.id != "local",
                node.display_name.casefold(),
                node.id,
            ),
        )

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def register_node(self, node: Node, *, persist: bool = True) -> Node:
        if not node.id:
            raise ValueError("node id cannot be empty")
        self.nodes[node.id] = node
        if persist:
            self.save_state()
        return node

    def set_node_enabled(self, node_id: str, enabled: bool) -> Node:
        if node_id == "local":
            raise ValueError("the local node cannot be disabled")
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Unknown node id: {node_id}")
        node.enabled = enabled
        if not enabled:
            for user_id, selected in tuple(self.selected_node_ids.items()):
                if selected == node_id:
                    self.selected_node_ids[user_id] = "local"
        self.save_state()
        return node

    def remove_node(self, node_id: str) -> Node:
        """Remove a remote node from the leader registry, preserving sessions."""
        if node_id == "local":
            raise ValueError("the local node cannot be removed")
        node = self.nodes.pop(node_id, None)
        if node is None:
            raise KeyError(f"Unknown node id: {node_id}")
        self.removed_node_ids.add(node_id)
        user_ids = (
            set(self.selected_node_ids)
            | set(self.active_sessions_by_node)
            | set(self.active_sessions)
        )
        for user_id in user_ids:
            selected = self.selected_node_ids.get(user_id, "local")
            if selected == node_id:
                self.selected_node_ids[user_id] = "local"
            node_sessions = self.active_sessions_by_node.get(user_id, {})
            node_sessions.pop(node_id, None)
            active_session_id = self.active_sessions.get(user_id)
            active_session = (
                self.sessions.get(active_session_id) if active_session_id else None
            )
            if active_session_id and (
                active_session is None or active_session.node_id == node_id
            ):
                local_session_id = node_sessions.get("local")
                if local_session_id:
                    self.active_sessions[user_id] = local_session_id
                else:
                    self.active_sessions.pop(user_id, None)
        self.save_state()
        return node

    def get_selected_node_id(self, user_id: int) -> str:
        selected = self.selected_node_ids.get(user_id, "local")
        return selected if selected in self.nodes else "local"

    def get_selected_node(self, user_id: int) -> Node:
        return self.nodes[self.get_selected_node_id(user_id)]

    def get_effective_backends(self, user_id: int, node_id: str) -> tuple[str, ...]:
        """Return backends currently admissible on one concrete node.

        Existing ``enabled_backends`` settings describe the local runtime.  A
        remote worker is authoritative for its own ready backend set; applying
        the leader's local setting there would hide worker-only providers.
        """
        node = self.nodes.get(node_id)
        if node is None or not node.is_available():
            return ()
        if node_id == "local":
            preferences = self.get_enabled_backends(user_id)
            # Persisted legacy state has no local capability snapshot. Once a
            # local readiness publisher fills ``Node.backends``, intersect it
            # with the user's local preferences exactly like a remote node.
            if node.backends:
                return tuple(
                    backend for backend in preferences if backend in node.backends
                )
            return tuple(preferences)
        return tuple(
            dict.fromkeys(
                backend
                for backend in node.backends
                if backend in ("claude", "codex")
            )
        )

    def set_selected_node(self, user_id: int, node_id: str) -> None:
        if node_id not in self.nodes:
            raise KeyError(f"Unknown node id: {node_id}")
        self.selected_node_ids[user_id] = node_id
        selected_session_id = self.active_sessions_by_node.get(user_id, {}).get(node_id)
        if selected_session_id:
            self.active_sessions[user_id] = selected_session_id
        else:
            self.active_sessions.pop(user_id, None)
        self.save_state()

    def start_context_transfer(
        self,
        *,
        source_session_id: str,
        target_node_id: str,
        target_backend: str,
        context_path: str,
        user_id: int = 0,
    ) -> SessionTransfer:
        """Create an idempotent leader-side transfer record."""
        source = self.sessions.get(source_session_id)
        target = self.nodes.get(target_node_id)
        if source is None:
            raise KeyError(f"Unknown source session: {source_session_id}")
        if source.state not in ("active", "idle"):
            raise ValueError("Only a live session can be transferred")
        if target is None:
            raise KeyError(f"Unknown target node: {target_node_id}")
        target_backends = self.get_effective_backends(user_id, target_node_id)
        if target_backend not in target_backends:
            raise ValueError("Target backend is unavailable on the target node")
        for transfer in self.transfers.values():
            if transfer.source_session_id == source_session_id and transfer.state in (
                "pending",
                "starting",
            ):
                return transfer
        transfer = SessionTransfer(
            id=SessionTransfer.new_id(),
            source_session_id=source.id,
            source_node_id=source.node_id,
            target_node_id=target_node_id,
            target_backend=target_backend,
            context_path=context_path,
            created_at=time.time(),
        )
        self.transfers[transfer.id] = transfer
        self.save_state()
        return transfer

    def fail_context_transfer(self, transfer_id: str, error: str) -> SessionTransfer:
        """Persist a terminal transfer failure without changing sessions."""
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            raise KeyError(f"Unknown transfer id: {transfer_id}")
        transfer.state = "failed"
        transfer.error = error
        self.save_state()
        return transfer

    def complete_context_transfer(
        self,
        transfer_id: str,
        *,
        user_id: int,
        context_error: str = "",
        target_window_id: str = "",
        target_workdir: str = "",
        target_agent_session_id: str = "",
        target_context_path: str = "",
    ) -> Session:
        """Create and select an independent target while preserving the source."""
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            raise KeyError(f"Unknown transfer: {transfer_id}")
        if transfer.state == "ready":
            target = self.sessions.get(transfer.target_session_id)
            if target is None:
                raise RuntimeError("Completed transfer has no target session")
            return target
        if transfer.state not in ("pending", "starting"):
            raise ValueError(f"Transfer is not completable: {transfer.state}")
        source = self.sessions.get(transfer.source_session_id)
        if source is None or source.state not in ("active", "idle"):
            raise ValueError("Source session is no longer live")
        target = self.create_session(
            name=source.name,
            window_id=target_window_id,
            workdir=target_workdir or source.workdir,
            backend=transfer.target_backend,
            node_id=transfer.target_node_id,
        )
        target.context_path = target_context_path or transfer.context_path
        target.context_error = context_error
        target.imported_from_backend = source.backend
        target.imported_from_session_id = source.id
        target.worker_session_id = target_agent_session_id
        transfer.target_session_id = target.id
        transfer.state = "ready"
        transfer.error = context_error
        self.set_selected_node(user_id, transfer.target_node_id)
        self.set_active_session(user_id, target.id)
        self.save_state()
        return target
