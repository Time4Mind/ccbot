"""Regression tests for voice_handler's session-pinning + reaction parity.

1. A voice message must be routed to the session that was active at the
   moment the bot received it, not whatever session is active by the
   time transcription finishes. ``voice_handler`` reads
   ``active_window(user.id)`` into a local ``wid`` BEFORE the slow
   ``transcribe_voice`` await; the transcribed text is later dispatched
   via that same captured ``wid``. Later content for that session is held
   by a per-session barrier, while session-switch callbacks remain
   responsive during transcription.

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

import asyncio
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


def _make_text_update(user_id: int = 1, text: str = "follow-up") -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = 22
    return update


class TestVoiceMessageOrdering:
    @pytest.mark.asyncio
    async def test_text_waits_until_prior_voice_is_dispatched(self):
        """A later text update cannot reach its routing path before the
        preceding voice update has completed transcription and dispatch."""
        voice_update = _make_voice_update()
        text_update = _make_text_update()
        context = _make_context()
        voice_started = asyncio.Event()
        finish_voice = asyncio.Event()
        events: list[str] = []

        async def _slow_voice(*args, **kwargs):
            events.append("voice-start")
            voice_started.set()
            await finish_voice.wait()
            events.append("voice-dispatched")

        async def _dispatch_text(*args, **kwargs):
            events.append("text-dispatched")

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.active_window", return_value="@5"),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages._process_voice",
                new=AsyncMock(side_effect=_slow_voice),
            ),
            patch(
                "ccbot.bot.messages.maybe_consume_code",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "ccbot.bot.messages._route_reply_quote",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "ccbot.bot.messages._resolve_active_window",
                new=AsyncMock(return_value="@5"),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch(
                "ccbot.bot.messages._intercept_if_pending_ui",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "ccbot.bot.messages._dispatch_text_to_active",
                new=AsyncMock(side_effect=_dispatch_text),
            ),
        ):
            from ccbot.bot.messages import text_handler, voice_handler

            voice_task = asyncio.create_task(voice_handler(voice_update, context))
            await voice_started.wait()
            text_task = asyncio.create_task(text_handler(text_update, context))
            await asyncio.sleep(0)

            assert events == ["voice-start"]
            finish_voice.set()
            await asyncio.gather(voice_task, text_task)

        assert events == ["voice-start", "voice-dispatched", "text-dispatched"]


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
        mock_sm.find_session_by_window.return_value = None

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


class TestVoicePendingCardMarker:
    @pytest.mark.asyncio
    async def test_card_reposted_with_pending_marker_before_transcription(self):
        """When the window has a bound Session, the live card reposts
        immediately (before transcription) with voice_pending set, and
        the flag is cleared again once transcription completes."""
        update = _make_voice_update()
        context = _make_context()
        events: list[str] = []

        mock_sm = MagicMock()
        mock_sm.get_active_window.return_value = "@5"
        pinned_sess = MagicMock()
        pinned_sess.id = "sess1"
        pinned_sess.name = "scraper"
        mock_sm.find_session_by_window.return_value = pinned_sess

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        from ccbot.handlers.card_model import CardState

        state = CardState()

        async def _repost_card(bot, user_id, sess):
            events.append(f"repost_card:voice_pending={state.voice_pending}")

        async def _transcribe(*args, **kwargs):
            events.append(f"transcribe:voice_pending={state.voice_pending}")
            return "hello"

        mock_dispatch = AsyncMock()

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
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
            patch("ccbot.bot.messages.get_card_state", return_value=state),
            patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
            patch(
                "ccbot.bot.messages.repost_card",
                new=AsyncMock(side_effect=_repost_card),
            ),
            patch("ccbot.bot.messages._dispatch_text_to_active", new=mock_dispatch),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        # The card was REPOSTED (fresh message below the user's voice)
        # with the pending marker set, and that repost happened before
        # transcription started. An in-place edit is not enough: the card
        # sits above the voice message the user just sent, so the user
        # would see nothing at all for the whole transcription.
        assert "repost_card:voice_pending=True" in events
        assert "transcribe:voice_pending=True" in events
        assert events.index("repost_card:voice_pending=True") < events.index(
            "transcribe:voice_pending=True"
        )
        # Cleared again once transcription finished.
        assert state.voice_pending is False

    @pytest.mark.asyncio
    async def test_marker_cleared_on_transcription_failure(self):
        """A failed transcription must not leave the card stuck showing
        the pending marker forever."""
        update = _make_voice_update()
        context = _make_context()

        mock_sm = MagicMock()
        mock_sm.get_active_window.return_value = "@5"
        pinned_sess = MagicMock()
        pinned_sess.id = "sess1"
        mock_sm.find_session_by_window.return_value = pinned_sess

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")

        from ccbot.handlers.card_model import CardState

        state = CardState()

        with (
            patch("ccbot.bot.messages.is_user_allowed", return_value=True),
            patch("ccbot.bot.messages.session_manager", mock_sm),
            patch("ccbot.bot._common.session_manager", mock_sm),
            patch("ccbot.bot.messages.tmux_manager", mock_tmux),
            patch("ccbot.bot.messages.resolve_voice_backend", return_value="whisper"),
            patch(
                "ccbot.bot.messages.transcribe_voice",
                new=AsyncMock(side_effect=ValueError("bad audio")),
            ),
            patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
            patch("ccbot.bot.messages.safe_reply", new=AsyncMock()),
            patch("ccbot.bot.messages.get_card_state", return_value=state),
            patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
            patch("ccbot.bot.messages.repost_card", new=AsyncMock()),
        ):
            from ccbot.bot.messages import voice_handler

            await voice_handler(update, context)

        assert state.voice_pending is False
