"""Queued inbound requests wait for terminal prompts instead of being dropped."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_queued_request_resumes_after_pending_approval_clears() -> None:
    approval = (
        "Would you like to run the following command?\n"
        "$ python -m pytest\n"
        "› 1. Yes, proceed (y)\n"
        "  2. No (esc)\n"
        "Press enter to confirm or esc to cancel\n"
    )
    tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5")),
        capture_pane=AsyncMock(side_effect=[approval, "Working"]),
    )
    sess = SimpleNamespace(id="sess1")
    manager = SimpleNamespace(
        find_session_by_window=lambda _wid: sess,
        get_active_session=lambda _uid: sess,
    )
    enter_kb = AsyncMock()
    reply = AsyncMock()

    with (
        patch("ccbot.bot.messages.tmux_manager", tmux),
        patch("ccbot.bot.messages.session_manager", manager),
        patch("ccbot.bot.messages.enter_kb_mode", new=enter_kb),
        patch("ccbot.bot.messages.safe_reply", new=reply),
        patch("ccbot.bot.messages.asyncio.sleep", new=AsyncMock()) as pause,
    ):
        from ccbot.bot.messages import _intercept_if_pending_ui

        intercepted = await _intercept_if_pending_ui(
            AsyncMock(),
            42,
            "@5",
            SimpleNamespace(),
            wait_until_clear=True,
        )

    assert intercepted is False
    enter_kb.assert_awaited_once()
    pause.assert_awaited_once_with(0.25)
    reply.assert_not_awaited()
    assert tmux.capture_pane.await_count == 2
