from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery

from ccbot.bot.callbacks import nodes
from ccbot.node_models import Node


@pytest.mark.asyncio
async def test_unavailable_node_primary_button_only_shows_alert(monkeypatch):
    query = MagicMock(spec=CallbackQuery)
    query.data = "nd:use:worker-a"
    query.answer = AsyncMock()
    manager = MagicMock()
    manager.get_node.return_value = Node("worker-a", "Worker A", state="offline")
    monkeypatch.setattr(nodes, "session_manager", manager)

    assert await nodes.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=42)
    )

    query.answer.assert_awaited_once_with("Node is unavailable", show_alert=True)
    manager.set_selected_node.assert_not_called()


@pytest.mark.asyncio
async def test_connected_node_without_ready_backends_can_be_selected(monkeypatch):
    query = MagicMock(spec=CallbackQuery)
    query.data = "nd:use:worker-a"
    query.answer = AsyncMock()
    manager = MagicMock()
    manager.get_node.return_value = Node(
        "worker-a",
        "Worker A",
        state="online",
        last_seen_at=time.time(),
        configured_backends=["claude", "codex"],
        backends=[],
    )
    open_sessions = AsyncMock()
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes, "open_sessions_in_place", open_sessions)
    context = SimpleNamespace(bot=object(), user_data={})

    assert await nodes.handle(query, context, SimpleNamespace(id=42))

    manager.set_selected_node.assert_called_once_with(42, "worker-a")
    query.answer.assert_awaited_once_with()
    open_sessions.assert_awaited_once_with(query, context.bot, 42)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, last_seen, enabled",
    [
        ("pending", 0.0, True),
        ("ready", 0.0, True),
        ("online", 1.0, True),
        ("online", None, False),
    ],
)
async def test_node_selection_preserves_connection_and_admin_guards(
    monkeypatch, state, last_seen, enabled
):
    query = MagicMock(spec=CallbackQuery)
    query.data = "nd:use:worker-a"
    query.answer = AsyncMock()
    manager = MagicMock()
    manager.get_node.return_value = Node(
        "worker-a",
        "Worker A",
        state=state,
        enabled=enabled,
        last_seen_at=time.time() if last_seen is None else last_seen,
    )
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes, "open_sessions_in_place", AsyncMock())

    assert await nodes.handle(
        query, SimpleNamespace(bot=object(), user_data={}), SimpleNamespace(id=42)
    )

    manager.set_selected_node.assert_not_called()
    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_delete_revokes_relay_credential_before_removing_node(monkeypatch):
    order = []
    query = MagicMock(spec=CallbackQuery)
    query.data = "nd:del:y:worker-a"
    query.answer = AsyncMock(
        side_effect=lambda *_args, **_kwargs: order.append("answer")
    )
    runtime = SimpleNamespace(
        revoke_node=AsyncMock(
            side_effect=lambda *_args, **_kwargs: order.append("revoke") or {"ok": True}
        )
    )
    manager = MagicMock()
    manager.get_node.return_value = Node("worker-a", "Worker A")
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(nodes, "unregister_node_runtime", lambda _node_id: None)
    monkeypatch.setattr(nodes, "safe_edit", AsyncMock())
    monkeypatch.setattr(nodes, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes, "build_nodes_keyboard", lambda _uid: None)

    assert await nodes.handle(query, SimpleNamespace(), SimpleNamespace(id=42))

    runtime.revoke_node.assert_awaited_once_with("worker-a")
    manager.remove_node.assert_called_once_with("worker-a")
    assert order[:2] == ["answer", "revoke"]
