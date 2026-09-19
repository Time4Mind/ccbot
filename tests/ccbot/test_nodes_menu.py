from __future__ import annotations

from ccbot.handlers import nodes
from ccbot.node_models import Node
from ccbot.session import session_manager


def test_nodes_menu_renders_status_and_selected_node(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes.session_manager,
        "list_nodes",
        lambda: [
            Node.local(),
            Node(
                id="office",
                display_name="Office Mac",
                state="offline",
                backends=["claude"],
                health_reason="relay timeout",
            ),
        ],
    )
    monkeypatch.setattr(
        nodes.session_manager, "get_selected_node_id", lambda _uid: "local"
    )
    monkeypatch.setattr(
        nodes.session_manager, "get_user_settings", lambda _uid: {"language": "ru"}
    )

    text = nodes.render_nodes_text(42)
    keyboard = nodes.build_nodes_keyboard(42)

    assert "✓ local" in text
    assert "Office Mac" in text
    assert "оффлайн" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "nd:use:local"
    assert keyboard.inline_keyboard[1][0].callback_data is None
    assert keyboard.inline_keyboard[1][1].callback_data == "nd:del:office"


def test_remove_node_preserves_local_registry_and_session_data(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "nodes",
        {"local": Node.local(), "office": Node("office", "Office")},
    )
    monkeypatch.setattr(session_manager, "selected_node_ids", {42: "office"})
    monkeypatch.setattr(session_manager, "active_sessions", {42: "remote-session"})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {42: {}})
    monkeypatch.setattr(session_manager, "sessions", {})
    monkeypatch.setattr(session_manager, "save_state", lambda: None)

    removed = session_manager.remove_node("office")

    assert removed.id == "office"
    assert [node.id for node in session_manager.list_nodes()] == ["local"]
    assert session_manager.get_selected_node_id(42) == "local"
    assert 42 not in session_manager.active_sessions
