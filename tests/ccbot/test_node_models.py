from __future__ import annotations

from ccbot.node_models import Node
from ccbot.session import SessionManager
from ccbot.session_models import Session, WindowState


def test_node_round_trip_preserves_runtime_capabilities() -> None:
    node = Node(
        id="office",
        display_name="Office Mac",
        state="ready",
        platform="darwin",
        arch="arm64",
        backends=["claude", "codex"],
        capabilities={"screenshot": True, "directory_browser": True},
        protocol_version="1",
        ccbot_version="0.2.0",
        last_seen_at=123.5,
        health_reason="",
    )

    restored = Node.from_dict(node.to_dict())

    assert restored == node


def test_legacy_session_and_window_state_default_to_local_node() -> None:
    session = Session.from_dict({"id": "legacy", "name": "Legacy"})
    window = WindowState.from_dict({"session_id": "provider"})

    assert session.node_id == "local"
    assert window.node_id == "local"


def test_node_scoped_session_state_round_trips_and_selects_node(
    monkeypatch,
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()
    remote = Node(
        id="office",
        display_name="Office Mac",
        state="ready",
        backends=["claude"],
    )
    manager.register_node(remote)

    session = manager.create_session(name="remote", node_id="office")
    manager.set_active_session(42, session.id)
    manager.set_selected_node(42, "office")

    assert manager.get_selected_node(42).id == "office"
    assert manager.get_active_session(42) is session
    assert manager.list_user_sessions(42) == [session]


def test_registering_second_node_is_the_visibility_boundary(monkeypatch) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()

    assert manager.registered_node_count == 1

    manager.register_node(
        Node(id="office", display_name="Office Mac", state="offline")
    )

    assert manager.registered_node_count == 2
    assert manager.has_multiple_nodes is True


def test_archiving_remote_session_clears_node_scoped_active_pointer(monkeypatch) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()
    manager.register_node(Node(id="office", display_name="Office", state="ready"))
    manager.set_selected_node(42, "office")
    session = manager.create_session(name="remote", node_id="office")
    manager.set_active_session(42, session.id)

    manager.mark_session_archived(session.id)

    assert manager.get_active_session(42) is None
