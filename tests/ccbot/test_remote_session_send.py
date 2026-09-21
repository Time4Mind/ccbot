from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.node_models import Node
from ccbot.session import SessionManager


@pytest.mark.asyncio
async def test_send_to_window_routes_remote_session_through_worker(monkeypatch):
    session = SimpleNamespace(
        node_id="worker-a",
        worker_session_id="routing-8",
        claude_session_id="provider-8",
        backend="claude",
    )
    runtime = SimpleNamespace(send_text=AsyncMock(return_value={"ok": True}))
    manager = SimpleNamespace(
        find_session_by_window=lambda _window_id: session,
        get_display_name=lambda window_id: window_id,
        get_node=lambda _node_id: Node(
            id="worker-a", display_name="Worker A", state="ready"
        ),
    )
    monkeypatch.setattr(
        "ccbot.transfer_runtime.get_node_runtime", lambda _node: runtime
    )

    result = await SessionManager.send_to_window(manager, "worker-a::@8", "hello")

    assert result == (True, "Sent to worker-a::@8")
    runtime.send_text.assert_awaited_once_with("worker-a", "routing-8", "hello")


@pytest.mark.asyncio
async def test_send_to_stale_remote_node_is_rejected_explicitly(monkeypatch):
    session = SimpleNamespace(
        node_id="worker-a",
        worker_session_id="routing-8",
        claude_session_id="provider-8",
        backend="claude",
    )
    runtime = SimpleNamespace(send_text=AsyncMock(return_value={"ok": True}))
    manager = SimpleNamespace(
        find_session_by_window=lambda _window_id: session,
        get_display_name=lambda window_id: window_id,
        get_node=lambda _node_id: Node(
            id="worker-a",
            display_name="Worker A",
            state="ready",
            last_seen_at=1.0,
        ),
    )
    monkeypatch.setattr(
        "ccbot.transfer_runtime.get_node_runtime", lambda _node: runtime
    )

    result = await SessionManager.send_to_window(manager, "worker-a::@8", "hello")

    assert result == (False, "Remote node is offline; message was not sent")
    runtime.send_text.assert_not_awaited()
