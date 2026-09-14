"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import status_poll_loop, update_status_message
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import (
        _active_interactive_window,
        _interactive_msgs,
    )

    _active_interactive_window.clear()
    _interactive_msgs.clear()
    yield
    _active_interactive_window.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(mock_bot, user_id=1, window_id=window_id)

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(mock_bot, user_id=1, window_id=window_id)

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await update_status_message(mock_bot, user_id=100, window_id=window_id)

            # Verify bot.send_message was called with keyboard.
            # In DM mode chat_id == user_id; no message_thread_id is passed.
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert "message_thread_id" not in call_kwargs
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_pre_resolved_window_skips_second_tmux_lookup(mock_bot: AsyncMock):
    """A background tick reuses its window snapshot without changing pane checks."""
    window_id = "@5"
    window = TmuxWindow(window_id, "work", "/tmp", "codex")

    with patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux:
        mock_tmux.find_window_by_id = AsyncMock()
        mock_tmux.capture_pane = AsyncMock(return_value="plain terminal output")

        await update_status_message(
            mock_bot,
            user_id=1,
            window_id=window_id,
            window=window,
        )

        mock_tmux.find_window_by_id.assert_not_awaited()
        mock_tmux.capture_pane.assert_awaited_once_with(window_id)


@pytest.mark.asyncio
async def test_status_tick_uses_one_window_snapshot_for_all_live_sessions():
    """One poll tick has fixed discovery cost instead of one scan per session."""
    sessions = [
        SimpleNamespace(id="s1", window_id="@1"),
        SimpleNamespace(id="s2", window_id="@2"),
    ]
    windows = [
        TmuxWindow("@1", "one", "/one", "codex"),
        TmuxWindow("@2", "two", "/two", "codex"),
    ]

    with (
        patch("ccbot.handlers.status_polling.config.allowed_users", {7}),
        patch(
            "ccbot.handlers.status_polling.session_manager.list_user_sessions",
            return_value=sessions,
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.polling_snapshot",
            new_callable=AsyncMock,
            return_value=windows,
        ) as snapshot,
        patch(
            "ccbot.handlers.status_polling.tmux_manager.find_window_by_id",
            new_callable=AsyncMock,
        ) as find_window,
        patch(
            "ccbot.handlers.status_polling.tmux_manager.capture_panes",
            new_callable=AsyncMock,
            return_value={"@1": "one pane", "@2": "two pane"},
        ) as capture,
        patch(
            "ccbot.handlers.status_polling.update_status_message",
            new_callable=AsyncMock,
        ) as update,
        patch(
            "ccbot.handlers.status_polling.idle_archive_sweep",
            new_callable=AsyncMock,
        ),
        patch("ccbot.handlers.status_polling.session_manager.save_state") as save_state,
        patch("ccbot.handlers.status_polling.purge_sweep"),
        patch("ccbot.handlers.status_polling.inbox_sweep"),
        patch(
            "ccbot.handlers.status_polling.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await status_poll_loop(AsyncMock())

    snapshot.assert_awaited_once_with()
    capture.assert_awaited_once_with(["@1", "@2"])
    find_window.assert_not_awaited()
    assert update.await_count == 2
    assert [call.kwargs["window"] for call in update.await_args_list] == windows
    save_state.assert_called_once_with()


@pytest.mark.asyncio
async def test_unchanged_windows_reuse_pane_text_without_recapture():
    sessions = [SimpleNamespace(id="s1", window_id="@1")]
    window = TmuxWindow("@1", "one", "/one", "codex", activity=100)

    with (
        patch("ccbot.handlers.status_polling.config.allowed_users", {7}),
        patch(
            "ccbot.handlers.status_polling.session_manager.list_user_sessions",
            return_value=sessions,
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.polling_snapshot",
            new_callable=AsyncMock,
            return_value=[window],
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.capture_panes",
            new_callable=AsyncMock,
            return_value={"@1": "cached pane"},
        ) as capture,
        patch(
            "ccbot.handlers.status_polling.update_status_message",
            new_callable=AsyncMock,
        ) as update,
        patch(
            "ccbot.handlers.status_polling.idle_archive_sweep",
            new_callable=AsyncMock,
        ),
        patch("ccbot.handlers.status_polling.purge_sweep"),
        patch("ccbot.handlers.status_polling.inbox_sweep"),
        patch("ccbot.handlers.status_polling.ACTIVITY_CAPTURE_GRACE", 0.0),
        patch(
            "ccbot.handlers.status_polling.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=[None, asyncio.CancelledError],
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await status_poll_loop(AsyncMock())

    capture.assert_awaited_once_with(["@1"])
    assert update.await_count == 1
    assert [call.kwargs["pane_text"] for call in update.await_args_list] == [
        "cached pane",
    ]


@pytest.mark.asyncio
async def test_failed_capture_of_changed_window_is_retried():
    sessions = [SimpleNamespace(id="s1", window_id="@1")]
    windows = [
        TmuxWindow("@1", "one", "/one", "codex", activity=100),
        TmuxWindow("@1", "one", "/one", "codex", activity=101),
        TmuxWindow("@1", "one", "/one", "codex", activity=101),
    ]

    with (
        patch("ccbot.handlers.status_polling.config.allowed_users", {7}),
        patch(
            "ccbot.handlers.status_polling.session_manager.list_user_sessions",
            return_value=sessions,
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.polling_snapshot",
            new_callable=AsyncMock,
            side_effect=[[windows[0]], [windows[1]], [windows[2]]],
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.capture_panes",
            new_callable=AsyncMock,
            side_effect=[{"@1": "old"}, {}, {"@1": "new"}],
        ) as capture,
        patch(
            "ccbot.handlers.status_polling.update_status_message",
            new_callable=AsyncMock,
        ) as update,
        patch(
            "ccbot.handlers.status_polling.idle_archive_sweep",
            new_callable=AsyncMock,
        ),
        patch("ccbot.handlers.status_polling.purge_sweep"),
        patch("ccbot.handlers.status_polling.inbox_sweep"),
        patch("ccbot.handlers.status_polling.ACTIVITY_CAPTURE_GRACE", 0.0),
        patch(
            "ccbot.handlers.status_polling.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=[None, None, asyncio.CancelledError],
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await status_poll_loop(AsyncMock())

    assert capture.await_count == 3
    assert [call.kwargs["pane_text"] for call in update.await_args_list] == [
        "old",
        "new",
    ]


@pytest.mark.asyncio
async def test_unchanged_window_gets_periodic_status_recheck():
    sessions = [SimpleNamespace(id="s1", window_id="@1")]
    window = TmuxWindow("@1", "one", "/one", "codex", activity=100)

    with (
        patch("ccbot.handlers.status_polling.config.allowed_users", {7}),
        patch(
            "ccbot.handlers.status_polling.session_manager.list_user_sessions",
            return_value=sessions,
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.polling_snapshot",
            new_callable=AsyncMock,
            return_value=[window],
        ),
        patch(
            "ccbot.handlers.status_polling.tmux_manager.capture_panes",
            new_callable=AsyncMock,
            return_value={"@1": "cached pane"},
        ) as capture,
        patch(
            "ccbot.handlers.status_polling.update_status_message",
            new_callable=AsyncMock,
        ) as update,
        patch(
            "ccbot.handlers.status_polling.idle_archive_sweep",
            new_callable=AsyncMock,
        ),
        patch("ccbot.handlers.status_polling.purge_sweep"),
        patch("ccbot.handlers.status_polling.inbox_sweep"),
        patch("ccbot.handlers.status_polling.ACTIVITY_CAPTURE_GRACE", 0.0),
        patch("ccbot.handlers.status_polling.UNCHANGED_STATUS_RECHECK", 0.0),
        patch(
            "ccbot.handlers.status_polling.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=[None, asyncio.CancelledError],
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await status_poll_loop(AsyncMock())

    capture.assert_awaited_once_with(["@1"])
    assert update.await_count == 2
