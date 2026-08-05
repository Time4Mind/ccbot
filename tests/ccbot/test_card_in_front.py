"""Regression tests for the "live card is always in front" invariants.

Three guarantees, all reported as broken from real usage:

1. **Background sessions never post their own card.** A voice message
   pins its tmux window at receipt; whisper can take 30-50 s, by which
   time the user may have switched to another session. The transcribed
   text must still reach the pinned pane, but the pinned session is a
   *background* one now — it must not drop a fresh card as the newest
   chat message (which also stole the live switcher and made the next
   switcher tap appear to edit "the previous message").

2. **One live switcher per chat.** ``_send_card`` strips the keyboard
   off every *other* known card message, not just the single per-user
   ``last_switcher_msg_id`` pointer — that pointer misses cards which
   ``_edit_card`` re-keyboarded without moving it.

3. **No double repost per user message.** ``card_is_below`` lets the
   dispatch skip a second repost when the voice flow already put the
   card below this very message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.card_model import CardState


def _make_update(user_id: int = 1, message_id: int = 500) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_id = message_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestBackgroundDispatchKeepsQuiet:
    @pytest.mark.asyncio
    async def test_bg_session_gets_no_card_only_a_panel_row(self):
        """Voice pinned to session A, user switched to B mid-transcription:
        text lands in A's pane, but A posts nothing to the chat."""
        update = _make_update()
        context = _make_context()

        pinned = MagicMock()
        pinned.id = "sessA"

        mock_sm = MagicMock()
        mock_sm.find_session_by_window.return_value = pinned
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        repost = AsyncMock()
        resume = AsyncMock()
        refresh = AsyncMock()

        with (
            patch("ccbot.bot.messages.session_manager", mock_sm),
            # A is NOT the active session any more.
            patch("ccbot.bot.messages.is_active_for_user", return_value=False),
            patch("ccbot.bot.messages.repost_card", new=repost),
            patch("ccbot.bot.messages.resume_card_view", new=resume),
            patch("ccbot.bot.messages.refresh_panel", new=refresh),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.get_interactive_window", return_value=None),
            patch(
                "ccbot.handlers.bg_status.update_status", return_value=True
            ) as bg_update,
        ):
            from ccbot.bot.messages import _dispatch_text_to_active

            await _dispatch_text_to_active(update, context, 1, "@5", "hi there")

        # Text still reached the pinned pane.
        mock_sm.send_to_window.assert_awaited_once_with("@5", "hi there")
        # ...but nothing was posted or repainted in chat for it.
        repost.assert_not_awaited()
        resume.assert_not_awaited()
        # Only the bg-status panel of the *active* card moved.
        bg_update.assert_called_once_with(1, "sessA", "working")
        refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bg_dispatch_does_not_arm_repost_intent(self):
        """The repost-intent buffer is an active-card mechanism; arming it
        for a bg session would silence that session's card until restart."""
        update = _make_update()
        context = _make_context()

        pinned = MagicMock()
        pinned.id = "sessA"

        mock_sm = MagicMock()
        mock_sm.find_session_by_window.return_value = pinned
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        with (
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot.messages.is_active_for_user", return_value=False),
            patch("ccbot.bot.messages.repost_card", new=AsyncMock()),
            patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
            patch("ccbot.bot.messages.refresh_panel", new=AsyncMock()),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.get_interactive_window", return_value=None),
            patch("ccbot.handlers.bg_status.update_status", return_value=False),
            patch("ccbot.bot.messages.begin_repost_intent") as begin,
        ):
            from ccbot.bot.messages import _dispatch_text_to_active

            await _dispatch_text_to_active(update, context, 1, "@5", "hi there")

        begin.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_during_dispatch_rechecks_before_repainting(self):
        """The session starts active, but loses the carrier while Codex is
        accepting the prompt. A stale pre-send owns_card value must not wake
        its paused card after the switch."""
        update = _make_update()
        context = _make_context()

        pinned = MagicMock()
        pinned.id = "sessA"
        pinned.backend = "codex"
        active = True

        async def _send_and_switch(*args, **kwargs):
            nonlocal active
            active = False
            return True, "ok"

        mock_sm = MagicMock()
        mock_sm.find_session_by_window.return_value = pinned
        mock_sm.send_to_window = AsyncMock(side_effect=_send_and_switch)

        repost = AsyncMock()
        resume = AsyncMock()
        refresh = AsyncMock()

        with (
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch(
                "ccbot.bot.messages.is_active_for_user",
                side_effect=lambda user_id, sess: active,
            ),
            patch("ccbot.bot.messages.repost_card", new=repost),
            patch("ccbot.bot.messages.resume_card_view", new=resume),
            patch("ccbot.bot.messages.refresh_panel", new=refresh),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.get_interactive_window", return_value=None),
            patch(
                "ccbot.bot.messages.tmux_manager.ensure_codex_prompt_submitted",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "ccbot.handlers.bg_status.update_status", return_value=True
            ) as bg_update,
        ):
            from ccbot.bot.messages import _dispatch_text_to_active

            await _dispatch_text_to_active(update, context, 1, "@5", "hi there")

        # Entry wakes the then-active card once. After the switch no second
        # resume/repost is allowed; only the new active card's bg panel moves.
        assert resume.await_count == 1
        repost.assert_not_awaited()
        bg_update.assert_called_once_with(1, "sessA", "working")
        refresh.assert_awaited_once()


class TestActiveDispatchPutsCardInFront:
    @pytest.mark.asyncio
    async def test_card_above_user_message_is_reposted(self):
        update = _make_update(message_id=500)
        context = _make_context()

        active = MagicMock()
        active.id = "sessA"

        mock_sm = MagicMock()
        mock_sm.find_session_by_window.return_value = active
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        repost = AsyncMock()

        with (
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot.messages.is_active_for_user", return_value=True),
            patch("ccbot.bot.messages.card_is_below", return_value=False),
            patch("ccbot.bot.messages.repost_card", new=repost),
            patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.get_interactive_window", return_value=None),
        ):
            from ccbot.bot.messages import _dispatch_text_to_active

            await _dispatch_text_to_active(update, context, 1, "@5", "hi there")

        repost.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_card_already_below_is_only_repainted(self):
        """Voice reposted the card at receipt — the post-transcription
        dispatch must not repost a second card for the same message."""
        update = _make_update(message_id=500)
        context = _make_context()

        active = MagicMock()
        active.id = "sessA"

        mock_sm = MagicMock()
        mock_sm.find_session_by_window.return_value = active
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        repost = AsyncMock()
        resume = AsyncMock()

        with (
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot.messages.is_active_for_user", return_value=True),
            patch("ccbot.bot.messages.card_is_below", return_value=True),
            patch("ccbot.bot.messages.repost_card", new=repost),
            patch("ccbot.bot.messages.resume_card_view", new=resume),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.get_interactive_window", return_value=None),
        ):
            from ccbot.bot.messages import _dispatch_text_to_active

            await _dispatch_text_to_active(update, context, 1, "@5", "hi there")

        repost.assert_not_awaited()
        # Once to un-pause before the send, once to drop the 🎙 marker.
        assert resume.await_count == 2


class TestCardIsBelow:
    def setup_method(self) -> None:
        notifications._cards.clear()

    def teardown_method(self) -> None:
        notifications._cards.clear()

    def test_true_when_card_msg_id_greater(self) -> None:
        notifications._cards[(1, "s")] = CardState(msg_id=501)
        assert notifications.card_is_below(1, "s", 500) is True

    def test_false_when_card_above(self) -> None:
        notifications._cards[(1, "s")] = CardState(msg_id=499)
        assert notifications.card_is_below(1, "s", 500) is False

    def test_false_without_card(self) -> None:
        assert notifications.card_is_below(1, "s", 500) is False

    def test_false_when_card_has_no_message(self) -> None:
        notifications._cards[(1, "s")] = CardState(msg_id=None)
        assert notifications.card_is_below(1, "s", 500) is False


class TestSingleLiveSwitcher:
    def setup_method(self) -> None:
        notifications._cards.clear()

    def teardown_method(self) -> None:
        notifications._cards.clear()

    @pytest.mark.asyncio
    async def test_strips_every_other_card_not_just_the_pointer(self) -> None:
        """Two live cards + a stale pointer → all three lose their
        keyboard, only the kept message stays tappable."""
        bot = AsyncMock()
        notifications._cards[(1, "keep")] = CardState(msg_id=900)
        notifications._cards[(1, "other")] = CardState(msg_id=880)
        notifications._cards[(2, "someone-else")] = CardState(msg_id=870)

        with patch.object(
            notifications.session_manager, "get_last_switcher_msg", return_value=860
        ):
            await notifications._strip_stale_switchers(bot, 1, 900, "keep")

        stripped = {
            call.kwargs["message_id"]
            for call in bot.edit_message_reply_markup.mock_calls
        }
        assert stripped == {880, 860}

    @pytest.mark.asyncio
    async def test_kb_mode_keyboard_is_never_stripped(self) -> None:
        """A card showing an AskUserQuestion grid must keep its keyboard —
        it is the surface the user has to act on, not a stale switcher."""
        bot = AsyncMock()
        notifications._cards[(1, "asking")] = CardState(msg_id=880, in_kb_mode=True)

        with patch.object(
            notifications.session_manager, "get_last_switcher_msg", return_value=None
        ):
            await notifications._strip_stale_switchers(bot, 1, 900, "keep")

        bot.edit_message_reply_markup.assert_not_called()
