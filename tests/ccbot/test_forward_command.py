"""Tests for forward_command_handler — slash-command forwarding to Claude.

DM mode: routing is via _active_window(user.id) which reads
session_manager.get_active_window. No thread_id is involved.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(text: str, user_id: int = 1) -> MagicMock:
    """Build a minimal mock Update for a private DM."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = user_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestForwardCommand:
    @pytest.mark.asyncio
    async def test_model_sends_command_to_tmux(self):
        """/model → send_to_window called with "/model"."""
        update = _make_update("/model")
        context = _make_context()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager") as mock_sm,
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager") as mock_tmux,
            patch("ccbot.bot.messages.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_active_window.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from ccbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/model")

    @pytest.mark.asyncio
    async def test_cost_sends_command_to_tmux(self):
        """/cost → send_to_window called with "/cost"."""
        update = _make_update("/cost")
        context = _make_context()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager") as mock_sm,
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager") as mock_tmux,
            patch("ccbot.bot.messages.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_active_window.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from ccbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/cost")

    @pytest.mark.asyncio
    async def test_clear_clears_session(self):
        """/clear → send_to_window + clear_window_session."""
        update = _make_update("/clear")
        context = _make_context()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager") as mock_sm,
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager") as mock_tmux,
            patch("ccbot.bot.messages.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_active_window.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from ccbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/clear")
            mock_sm.clear_window_session.assert_called_once_with("@5")
