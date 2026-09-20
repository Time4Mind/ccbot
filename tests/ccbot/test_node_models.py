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
        capacity={"active_sessions": 2, "max_sessions": 8},
        protocol_version="1",
        ccbot_version="0.2.0",
        last_seen_at=123.5,
        health_reason="",
    )

    restored = Node.from_dict(node.to_dict())

    assert restored == node


def test_remote_node_health_expires_without_changing_local_node() -> None:
    remote = Node(
        id="office",
        display_name="Office",
        state="ready",
        last_seen_at=100.0,
    )

    assert remote.is_available(now=144.9)
    assert not remote.is_available(now=145.0)
    assert Node.local().is_available(now=10_000.0)


def test_legacy_session_and_window_state_default_to_local_node() -> None:
    session = Session.from_dict({"id": "legacy", "name": "Legacy"})
    window = WindowState.from_dict({"session_id": "provider"})

    assert session.node_id == "local"
    assert window.node_id == "local"


def test_session_context_metadata_round_trips() -> None:
    session = Session(
        id="target",
        name="Imported",
        context_path="/var/lib/ccbot/imports/target-full.md",
        context_error="Backend context window is smaller than the imported context",
    )

    restored = Session.from_dict(session.to_dict())

    assert restored.context_path == session.context_path
    assert restored.context_error == session.context_error


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

    manager.register_node(Node(id="office", display_name="Office Mac", state="offline"))

    assert manager.registered_node_count == 2
    assert manager.has_multiple_nodes is True


def test_archiving_remote_session_clears_node_scoped_active_pointer(
    monkeypatch,
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()
    manager.register_node(Node(id="office", display_name="Office", state="ready"))
    manager.set_selected_node(42, "office")
    session = manager.create_session(name="remote", node_id="office")
    manager.set_active_session(42, session.id)

    manager.mark_session_archived(session.id)

    assert manager.get_active_session(42) is None


def test_context_transfer_creates_independent_target_and_keeps_source_live(
    monkeypatch,
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()
    manager.register_node(
        Node(
            id="office",
            display_name="Office",
            state="ready",
            backends=["codex"],
        )
    )
    source = manager.create_session(name="same title", backend="claude")
    manager.set_active_session(42, source.id)

    transfer = manager.start_context_transfer(
        source_session_id=source.id,
        target_node_id="office",
        target_backend="codex",
        context_path="/target/imports/full.md",
    )
    target = manager.complete_context_transfer(
        transfer.id,
        user_id=42,
        context_error="Context window is smaller",
    )

    assert target.id != source.id
    assert target.name == source.name
    assert target.node_id == "office"
    assert target.context_path == "/target/imports/full.md"
    assert target.context_error == "Context window is smaller"
    assert source.state in ("active", "idle")
    assert manager.get_active_session(42) is target


def test_transfer_rejects_backend_not_available_on_worker(monkeypatch) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    manager = SessionManager()
    manager.register_node(
        Node(id="office", display_name="Office", state="ready", backends=["claude"])
    )
    source = manager.create_session(name="source")

    try:
        manager.start_context_transfer(
            source_session_id=source.id,
            target_node_id="office",
            target_backend="codex",
            context_path="/target/imports/full.md",
        )
    except ValueError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("transfer admission must reject unavailable backend")
