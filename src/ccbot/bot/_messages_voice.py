"""Voice-message intake and transcription implementation.

Public imports remain in :mod:`ccbot.bot.messages`.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, Update
from telegram.error import NetworkError
from telegram.ext import ContextTypes

from ..handlers.message_sender import (
    safe_reply,
)
from ..handlers.notifications import (
    get_card_state,
    is_active_for_user,
    repost_card,
    resume_card_view,
)
from ..handlers.typing import fire_typing
from ..i18n import t
from ..session_models import Session
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..transcribe import resolve_voice_backend, transcribe_voice
from ._common import active_window, is_user_allowed

from typing import Any, TYPE_CHECKING, cast

__all__ = [
    "_clear_voice_pending_marker",
    "voice_handler",
    "_process_voice",
]

if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    _VOICE_DOWNLOAD_ATTEMPTS = cast(Any, None)
    _append_dropped_queue_notice = cast(Any, None)
    _dispatch_text_to_active = cast(Any, None)
    _download_voice_bytes = cast(Any, None)
    _enqueue_voice = cast(Any, None)
    _intercept_if_pending_ui = cast(Any, None)
    _pane_has_interactive_ui = cast(Any, None)
    _release_voice = cast(Any, None)
    _voice_transcript_checkpoint = cast(Any, None)
    _wait_for_voice = cast(Any, None)
    _wait_for_voice_transcript = cast(Any, None)
    cancel_bash_capture = cast(Any, None)

logger = logging.getLogger(__name__)
# --- voice ---


async def _clear_voice_pending_marker(bot: Bot, user_id: int, sess: Session) -> None:
    """Repaint the card without the temporary voice_pending user row.

    Only needed on the transcription-failure paths — the success path's
    ``_dispatch_text_to_active`` already reposts unconditionally, which
    naturally drops the marker once ``voice_pending`` is cleared.
    """
    try:
        await resume_card_view(bot, user_id, sess)
    except Exception as e:
        logger.debug("voice-pending marker clear failed: %s", e)


async def voice_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
    ordered: bool = False,
    surface_pending: bool = True,
) -> bool:
    """Queue a voice turn, then transcribe it without letting later messages pass."""
    user = update.effective_user
    if (
        not user
        or not is_user_allowed(user.id)
        or not update.message
        or not update.message.voice
        or resolve_voice_backend(user.id) == "off"
    ):
        return await _process_voice(update, context)

    wid = pinned_wid or active_window(user.id)
    if wid is None:
        return await _process_voice(update, context)

    if ordered:
        return await _process_voice(
            update,
            context,
            pinned_wid=wid,
            surface_pending=surface_pending,
        )

    previous, barrier = _enqueue_voice(user.id, wid)
    delivered = False
    try:
        if previous is not None and not await _wait_for_voice(previous):
            return False
        delivered = await _process_voice(
            update,
            context,
            pinned_wid=wid,
            queue_barrier=barrier,
            surface_pending=surface_pending,
        )
    finally:
        _release_voice(user.id, wid, barrier, delivered=delivered)
    return delivered


async def _process_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
    queue_barrier: asyncio.Future[bool] | None = None,
    surface_pending: bool = True,
) -> bool:
    """Transcribe the voice and forward as text to the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return False

    if not update.message or not update.message.voice:
        return False

    if resolve_voice_backend(user.id) == "off":
        await safe_reply(update.message, "⚠ Voice is disabled (voice backend = off).")
        return False
    wid = pinned_wid or active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return False

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return False

    # wid is pinned NOW, before the slow download/transcribe steps — a
    # switch afterwards can't redirect this voice message.
    await fire_typing(context.bot, user.id, "voice_handler.received", window_id=wid)

    # Same immediate reaction a typed message gets: the live card is
    # REPOSTED as a fresh message right now, below the voice the user
    # just sent — not edited in place. An in-place edit lands on a card
    # that sits ABOVE the voice message, which the user never sees; the
    # symptom was 35-50 s of apparent dead air while whisper ran (they
    # re-recorded, switched sessions, assumed it was broken).
    # ``voice_pending`` adds a synthetic trailing user row so the reposted
    # card says "voice received, already bound here" exactly where a typed
    # prompt would appear. The header remains stable.
    #
    # The cross-session repost race that made an earlier revision back
    # this out is handled properly now: ``_send_card`` serializes spawns
    # per user and strips every other card's keyboard, so two reposts
    # can no longer desync which message carries the live switcher.
    # Skipped for an orphan window (no Session record).
    sess = session_manager.find_session_by_window(wid)
    card_state = get_card_state(user.id, sess) if sess is not None else None
    if sess is not None and card_state is not None:
        card_state.voice_pending = True
        # A pagination tap may have left the card on an older page. A new
        # voice message is a new user turn, so focus the latest page just as
        # the normal prompt flow does before showing the pending row.
        card_state.current_page_idx = None
        if is_active_for_user(user.id, sess):
            try:
                if surface_pending:
                    await repost_card(context.bot, user.id, sess)
                else:
                    # Fast intake already moved the card below the voice. Edit
                    # that carrier to add the pending marker; reposting here
                    # would create a second acknowledgement card.
                    await resume_card_view(context.bot, user.id, sess)
            except Exception as e:
                logger.debug("voice-pending card surface failed: %s", e)

    try:
        ogg_data = await _download_voice_bytes(
            update.message.voice, user_id=user.id, wid=wid
        )
    except NetworkError as e:
        if sess is not None and card_state is not None:
            card_state.voice_pending = False
            await _clear_voice_pending_marker(context.bot, user.id, sess)
        logger.error(
            "Voice did not reach transcription after %d download attempts "
            "user=%d window=%s: %s",
            _VOICE_DOWNLOAD_ATTEMPTS,
            user.id,
            wid,
            e,
        )
        try:
            await safe_reply(
                update.message,
                _append_dropped_queue_notice(
                    user.id,
                    t(
                        user.id,
                        "voice.download_failed",
                        attempts=_VOICE_DOWNLOAD_ATTEMPTS,
                    ),
                    queue_barrier,
                ),
            )
        except Exception as notify_error:
            logger.warning(
                "Voice download failure notification failed user=%d window=%s: %s",
                user.id,
                wid,
                notify_error,
            )
        return False

    try:
        text = await transcribe_voice(ogg_data, user_id=user.id)
    except ValueError:
        if sess is not None and card_state is not None:
            card_state.voice_pending = False
            await _clear_voice_pending_marker(context.bot, user.id, sess)
        await safe_reply(
            update.message,
            _append_dropped_queue_notice(
                user.id, t(user.id, "voice.transcription_failed"), queue_barrier
            ),
        )
        return False
    except Exception as e:
        if sess is not None and card_state is not None:
            card_state.voice_pending = False
            await _clear_voice_pending_marker(context.bot, user.id, sess)
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(
            update.message,
            _append_dropped_queue_notice(
                user.id, t(user.id, "voice.transcription_failed"), queue_barrier
            ),
        )
        return False

    if card_state is not None:
        card_state.voice_pending = False

    # Typing is a chat-level indicator, so it only makes sense while the
    # pinned session is still the one the user is looking at. If they
    # switched away during transcription, the text still goes to the
    # pinned pane but must stay invisible in chat.
    if sess is not None and is_active_for_user(user.id, sess):
        await fire_typing(
            context.bot, user.id, "voice_handler.transcribed", window_id=wid
        )
    cancel_bash_capture(user.id, wid)

    # A transcription is expensive and unrecoverable — unlike typed text the
    # user can't just retype 90 seconds of speech. If the pane is showing an
    # interactive prompt, the text would be consumed as menu keystrokes and
    # silently lost, so tell the user to resend rather than swallowing it.
    _voice_lost_notice = _append_dropped_queue_notice(
        user.id, t(user.id, "voice.not_delivered"), queue_barrier
    )
    if await _intercept_if_pending_ui(
        context.bot, user.id, wid, update.message, _voice_lost_notice
    ):
        return False

    # Same dispatch path text uses — identical reaction (send, auto-name,
    # bash-capture, interactive-UI check, card repost) once the text is
    # known. No voice-specific reply; the transcribed text just becomes
    # this message's text, same as if the user had typed it.
    transcript_checkpoint = _voice_transcript_checkpoint(wid)
    dispatched = await _dispatch_text_to_active(update, context, user.id, wid, text)
    if dispatched is False:
        return False

    # A prompt appearing after send is not proof that the voice was eaten: it
    # can be an approval raised by the successfully delivered turn, especially
    # for the second voice in a queue. Prefer the authoritative transcript and
    # only use the pane heuristic when no matching user row appears.
    transcript_confirmed = await _wait_for_voice_transcript(
        transcript_checkpoint, text, wid=wid
    )
    if transcript_confirmed is True:
        logger.info(
            "Voice delivery confirmed by transcript user=%d window=%s",
            user.id,
            wid,
        )
        return True
    if transcript_confirmed is None:
        await asyncio.sleep(1.5)
    if await _pane_has_interactive_ui(wid):
        logger.warning(
            "Voice delivery unconfirmed while interactive UI is visible "
            "user=%d window=%s",
            user.id,
            wid,
        )
        try:
            await safe_reply(update.message, _voice_lost_notice)
        except Exception:
            pass
        return False
    return True
