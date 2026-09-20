from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.session import SessionManager


@pytest.mark.asyncio
async def test_send_to_window_routes_remote_session_through_worker(monkeypatch):
    session = SimpleNamespace(
        node_id="worker-a", claude_session_id="agent-8", backend="claude"
    )
    runtime = SimpleNamespace(send_text=AsyncMock(return_value={"ok": True}))
    manager = SimpleNamespace(
        find_session_by_window=lambda _window_id: session,
        get_display_name=lambda window_id: window_id,
    )
    monkeypatch.setattr(
        "ccbot.transfer_runtime.get_node_runtime", lambda _node: runtime
    )

    result = await SessionManager.send_to_window(manager, "worker-a::@8", "hello")

    assert result == (True, "Sent to worker-a::@8")
    runtime.send_text.assert_awaited_once_with("worker-a", "agent-8", "hello")
