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
    async def test_model_keeps_the_card_already_sent_by_intake(self):
        from ccbot.handlers import notifications
        from ccbot.session_models import Session

        update = _make_update("/model")
        update.message.message_id = 90
        context = _make_context()
        sess = Session(id="model-receipt", name="project", window_id="@5")
        state = notifications.CardState(msg_id=100)
        sent_cards = []

        async def send_card(_bot, _uid, _sess, target, **kwargs):
            sent_cards.append(kwargs["text"])
            target.msg_id = 101

        with (
            patch.dict(notifications._cards, {(1, sess.id): state}, clear=True),
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.is_active_for_user", return_value=True),
            patch("ccbot.bot.messages.session_manager") as manager,
            patch("ccbot.bot._common.session_manager", manager),
            patch(
                "ccbot.bot.messages.session_is_reachable",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "ccbot.bot.messages._intercept_if_pending_ui",
                new=AsyncMock(return_value=False),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
            patch.object(notifications, "_ensure_seeded", new=AsyncMock()),
            patch.object(notifications, "_render_card", return_value="model picker"),
            patch.object(notifications, "_send_card", side_effect=send_card),
        ):
            manager.get_active_window.return_value = "@5"
            manager.find_session_by_window.return_value = sess
            manager.send_to_window = AsyncMock(return_value=(True, "ok"))
            from ccbot.bot import forward_command_handler

            assert await forward_command_handler(update, context, pinned_wid="@5")

        assert state.msg_id == 100
        assert sent_cards == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command", ["history", "done", "memory", "compact", "effort"]
    )
    async def test_removed_commands_are_not_forwarded(self, command):
        update = _make_update(f"/{command}")
        context = _make_context()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager") as mock_sm,
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager") as mock_tmux,
            patch(
                "ccbot.bot.messages.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
        ):
            mock_sm.get_active_window.return_value = "@5"

            from ccbot.bot import forward_command_handler

            assert not await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_not_called()
            mock_tmux.find_window_by_id.assert_not_called()
            safe_reply.assert_awaited_once()

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
            patch("ccbot.bot.messages.is_active_for_user", return_value=True),
            patch("ccbot.bot.messages.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_active_window.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_sm.find_session_by_window.return_value = None
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
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_sm.find_session_by_window.return_value = None
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
            patch("ccbot.bot.messages.is_active_for_user", return_value=True),
            patch("ccbot.bot.messages.safe_reply", new_callable=AsyncMock),
            patch(
                "ccbot.bot.messages.clear_card", new_callable=AsyncMock
            ) as clear_card,
            patch(
                "ccbot.bot.messages.resume_card_view", new_callable=AsyncMock
            ) as resume_card,
        ):
            mock_sm.get_active_window.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_tmux.capture_pane = AsyncMock(return_value="")
            sess = MagicMock(id="sess")
            mock_sm.find_session_by_window.return_value = sess
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from ccbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/clear")
            mock_sm.clear_window_session.assert_called_once_with("@5")
            clear_card.assert_awaited_once_with(context.bot, 1, sess)
            # Once on bracket entry, once after the card is cleared.
            assert resume_card.await_count == 2
