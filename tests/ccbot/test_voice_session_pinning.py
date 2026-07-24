"""Regression tests for voice_handler's session-pinning + reaction parity.

1. A voice message must be routed to the session that was active at the
   moment the bot received it, not whatever session is active by the
   time transcription finishes. ``voice_handler`` reads
   ``active_window(user.id)`` into a local ``wid`` BEFORE the slow
   ``transcribe_voice`` await; the transcribed text is later dispatched
   via that same captured ``wid``. Combined with python-telegram-bot's
   default ``concurrent_updates=1`` (updates are processed strictly one
   at a time — see ``Application.__process_update_wrapper`` gating in
   ``process_update``), a session-switch callback that arrives after the
   voice message cannot even start processing until ``voice_handler``'s
   whole coroutine — including transcription — has finished.

2. The reaction must match what a typed text message gets: an instant
   typing indicator fired before the slow step (transcription for
   voice, nothing for text — text has no slow step of its own), and
   once the content is known, dispatch through the exact same
   ``_dispatch_text_to_active`` path text uses — same send, same
   auto-naming, same bash-capture check, same card repost. No
   voice-specific reply message; the transcribed text just becomes the
   message's text, same as if the user had typed it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_voice_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.voice = MagicMock()
    voice_file = MagicMock()
    voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg-bytes"))
    update.message.voice.get_file = AsyncMock(return_value=voice_file)
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


class TestVoiceSessionPinning:
    @pytest.mark.asyncio
    async def test_switch_during_transcription_does_not_redirect_voice(self):
        """Active session flips mid-transcription — voice still goes to
        the window that was active when the message was received."""
        update = _make_voice_update()
        context = _make_context()

        mock_sm = MagicMock()
        # Session "A" (window @5) is active when the voice arrives.
        mock_sm.get_active_window.return_value = "@5"
        mock_sm.find_session_by_window.return_value = None
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        async def _slow_transcribe(*args, **kwargs):
            # Simulate the user switching to session "B" (window @9)
            # WHILE transcription is still in flight.
            mock_sm.get_active_window.return_value = "@9"
            return "hello from voice"

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager", mock_tmux),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages.transcribe_voice",
                new=AsyncMock(side_effect=_slow_transcribe),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        # The active window changed to @9 during transcription, but the
        # transcribed text must still land on @5 — the window captured
        # BEFORE the slow transcribe call.
        mock_sm.send_to_window.assert_called_once_with("@5", "hello from voice")
        assert mock_sm.get_active_window.return_value == "@9"  # switch did happen

    @pytest.mark.asyncio
    async def test_no_switch_routes_to_active_window(self):
        """Sanity check: without a switch, voice goes to the active window."""
        update = _make_voice_update()
        context = _make_context()

        mock_sm = MagicMock()
        mock_sm.get_active_window.return_value = "@7"
        mock_sm.find_session_by_window.return_value = None
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@7"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager", mock_tmux),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages.transcribe_voice",
                new=AsyncMock(return_value="hi"),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        mock_sm.send_to_window.assert_called_once_with("@7", "hi")


class TestVoiceReactionParity:
    @pytest.mark.asyncio
    async def test_typing_fires_before_transcription(self):
        """The typing indicator must fire immediately on receipt — the
        same instant "message accepted" signal text gets — not only
        after transcription completes."""
        update = _make_voice_update()
        context = _make_context()
        events: list[str] = []

        mock_sm = MagicMock()
        mock_sm.get_active_window.return_value = "@5"
        mock_sm.find_session_by_window.return_value = None
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        async def _fire_typing(bot, user_id, source, **extra):
            events.append(f"typing:{source}")

        async def _transcribe(*args, **kwargs):
            events.append("transcribe")
            return "hello"

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager", mock_tmux),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages.transcribe_voice",
                new=AsyncMock(side_effect=_transcribe),
            ),
            patch(
                "ccbot.bot.messages.fire_typing",
                new=AsyncMock(side_effect=_fire_typing),
            ),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        assert "typing:voice_handler.received" in events
        assert events.index("typing:voice_handler.received") < events.index(
            "transcribe"
        )

    @pytest.mark.asyncio
    async def test_dispatches_through_same_path_as_text(self):
        """Once transcribed, the text is handed to _dispatch_text_to_active
        — the exact function text_handler uses — not a voice-specific
        send/repost flow."""
        update = _make_voice_update()
        context = _make_context()

        mock_sm = MagicMock()
        mock_sm.get_active_window.return_value = "@5"

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        mock_dispatch = AsyncMock()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager", mock_tmux),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages.transcribe_voice",
                new=AsyncMock(return_value="hello from voice"),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
            patch("ccbot.bot.messages._dispatch_text_to_active", new=mock_dispatch),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        mock_dispatch.assert_called_once_with(
            update, context, update.effective_user.id, "@5", "hello from voice"
        )
