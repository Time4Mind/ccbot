from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery

from ccbot.bot.callbacks import nodes
from ccbot.node_models import Node


@pytest.mark.asyncio
async def test_delete_revokes_relay_credential_before_removing_node(monkeypatch):
    order = []
    query = MagicMock(spec=CallbackQuery)
    query.data = "nd:del:y:worker-a"
    query.answer = AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("answer"))
    runtime = SimpleNamespace(
        revoke_node=AsyncMock(
            side_effect=lambda *_args, **_kwargs: (
                order.append("revoke") or {"ok": True}
            )
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
