from __future__ import annotations

from ccbot.handlers import nodes
from ccbot.node_models import Node


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
