from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import terminal_runtime
from ccbot.session_models import Session


@pytest.mark.asyncio
async def test_remote_pane_capture_routes_by_worker_id_not_composite_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        capture_session=AsyncMock(return_value={"ok": True, "pane": "same pane"})
    )
    local_capture = AsyncMock()
    sess = Session(
        id="remote",
        name="Remote",
        node_id="worker-a",
        window_id="worker-a::@17",
        worker_session_id="worker-session",
    )
    monkeypatch.setattr(terminal_runtime, "get_node_runtime", lambda _nid: runtime)
    monkeypatch.setattr(terminal_runtime.tmux_manager, "capture_panes", local_capture)

    pane = await terminal_runtime.capture_session_pane(sess, with_ansi=True)

    assert pane == "same pane"
    runtime.capture_session.assert_awaited_once_with(
        "worker-a", "worker-session", with_ansi=True
    )
    local_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_pane_capture_uses_same_logical_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_capture = AsyncMock(return_value={"@17": "same pane"})
    sess = Session(id="local", name="Local", window_id="@17")
    monkeypatch.setattr(terminal_runtime.tmux_manager, "capture_panes", local_capture)

    pane = await terminal_runtime.capture_session_pane(sess, with_ansi=True)

    assert pane == "same pane"
    local_capture.assert_awaited_once_with(["@17"], with_ansi=True)
