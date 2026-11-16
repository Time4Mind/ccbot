"""A manual model/mode selection holds queued input until the user finishes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

VARIANT = """          Select variant                     esc
              Search
              Default
              low
              medium
              high
  ┃
  ┃  Build · GPT-6.1 Sol OpenAI
  /tmp/lab 14K (1%) ctrl+p commands
"""


@pytest.mark.asyncio
async def test_variant_is_surfaced_and_never_automatically_answered(monkeypatch):
    from ccbot.bot import messages

    sess = SimpleNamespace(id="oc", backend="opencode", window_id="@5")
    manager = SimpleNamespace(
        find_session_by_window=lambda _: sess, get_active_session=lambda _: sess
    )
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(
            find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5")),
            capture_pane=AsyncMock(return_value=VARIANT),
        ),
    )
    keyboard = AsyncMock()
    monkeypatch.setattr(messages, "enter_kb_mode", keyboard)
    monkeypatch.setattr(messages, "safe_reply", AsyncMock())
    key = AsyncMock()
    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", key)
    assert await messages._intercept_if_pending_ui(
        AsyncMock(), 42, "@5", SimpleNamespace()
    )
    assert keyboard.await_args.args[-1] == "OpenCodeVariant"
    key.assert_not_awaited()
