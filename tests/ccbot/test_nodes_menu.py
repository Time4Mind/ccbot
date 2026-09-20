from __future__ import annotations

from ccbot.handlers import nodes
from ccbot.node_models import Node
from ccbot.session import session_manager


_BUTTON_ACTION_FIELDS = {
    "url",
    "callback_data",
    "web_app",
    "login_url",
    "switch_inline_query",
    "switch_inline_query_current_chat",
    "switch_inline_query_chosen_chat",
    "callback_game",
    "pay",
    "copy_text",
}


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
    assert keyboard.inline_keyboard[1][0].callback_data == "nd:use:office"
    assert keyboard.inline_keyboard[1][1].callback_data == "nd:off:office"
    assert keyboard.inline_keyboard[1][2].callback_data == "nd:del:office"


def test_every_node_menu_button_has_exactly_one_telegram_action(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes.session_manager,
        "list_nodes",
        lambda: [
            Node.local(),
            Node(id="ready", display_name="Ready", state="ready"),
            Node(
                id="stale",
                display_name="Stale",
                state="ready",
                last_seen_at=1.0,
            ),
            Node(id="offline", display_name="Offline", state="offline"),
            Node(
                id="disabled",
                display_name="Disabled",
                state="ready",
                enabled=False,
            ),
        ],
    )
    monkeypatch.setattr(
        nodes.session_manager, "get_selected_node_id", lambda _uid: "local"
    )
    monkeypatch.setattr(
        nodes.session_manager, "get_user_settings", lambda _uid: {"language": "en"}
    )

    keyboard = nodes.build_nodes_keyboard(42)

    for row in keyboard.inline_keyboard:
        for button in row:
            action_fields = _BUTTON_ACTION_FIELDS.intersection(button.to_dict())
            assert len(action_fields) == 1, button.to_dict()


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
    monkeypatch.setattr(session_manager, "removed_node_ids", set())
    monkeypatch.setattr(session_manager, "save_state", lambda: None)

    removed = session_manager.remove_node("office")

    assert removed.id == "office"
    assert [node.id for node in session_manager.list_nodes()] == ["local"]
    assert session_manager.get_selected_node_id(42) == "local"
    assert 42 not in session_manager.active_sessions
    assert "office" in session_manager.removed_node_ids


def test_disabled_node_remains_registered_but_cannot_accept_work(monkeypatch) -> None:
    node = Node("office", "Office", state="ready", last_seen_at=100.0)
    monkeypatch.setattr(
        session_manager, "nodes", {"local": Node.local(), "office": node}
    )
    monkeypatch.setattr(session_manager, "save_state", lambda: None)

    session_manager.set_node_enabled("office", False)

    assert session_manager.get_node("office") is node
    assert node.enabled is False
    assert node.is_available(now=100.0) is False
