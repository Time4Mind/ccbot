"""Shared inbound delivery, ordering and pending-UI implementation.
Imported through the monkeypatch-compatible :mod:`ccbot.bot.messages` facade."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import ContextTypes

from ..handlers.interactive_ui import (
    handle_interactive_ui,
)
from ..handlers.message_sender import (
    safe_reply,
)
from ..handlers.notifications import (
    begin_repost_intent,
    clear_card,
    end_repost_intent,
    enter_kb_mode,
    get_card_state,
    is_active_for_user,
    repost_card,
    resume_card_view,
)
from ..handlers.card_types import TurnPhase
from ..handlers.typing import fire_typing
from ..i18n import t
from ..session_models import Session, WindowState
from ..session import session_manager
from ..terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
)
from ..tmux_manager import tmux_manager
from ._common import active_window, is_user_allowed

__all__ = [
    "_voice_barriers",
    "_voice_waiters",
    "_VOICE_DOWNLOAD_ATTEMPTS",
    "_VOICE_DOWNLOAD_RETRY_DELAYS",
    "_VOICE_TRANSCRIPT_CONFIRM_TIMEOUT",
    "_VOICE_TRANSCRIPT_CONFIRM_POLL",
    "_VoiceTranscriptCheckpoint",
    "_voice_transcript_checkpoint",
    "_transcript_contains_voice_text",
    "_wait_for_voice_transcript",
    "_send_with_delivery_proof",
    "_enqueue_voice",
    "_wait_for_voice",
    "_await_prior_voice",
    "_release_voice",
    "_append_dropped_queue_notice",
    "_download_voice_bytes",
    "_FILE_TOO_BIG_MSG",
    "_is_file_too_big",
    "_RepostHandle",
    "_card_repost_bracket",
    "_pane_has_interactive_ui",
    "_intercept_if_pending_ui",
    "forward_command_handler",
]

logger = logging.getLogger(__name__)

# The tail of the voice-message chain for each session window.  Voice
# transcription runs in a non-blocking PTB handler, so later updates can enter
# their handlers while Whisper is still working.  Those handlers wait on the
# tail that existed when they arrived, preserving Telegram message order.
_voice_barriers: dict[tuple[int, str], asyncio.Future[bool]] = {}
_voice_waiters: dict[asyncio.Future[bool], int] = {}

# A voice update holds the per-session ordering barrier while these attempts
# run.  Retrying here is important: once Telegram has delivered the update,
# dropping a transient getFile/download failure would permanently lose that
# turn and let later messages overtake it.
_VOICE_DOWNLOAD_ATTEMPTS = 3
_VOICE_DOWNLOAD_RETRY_DELAYS = (1.0, 2.0)
_VOICE_TRANSCRIPT_CONFIRM_TIMEOUT = 15.0
_VOICE_TRANSCRIPT_CONFIRM_POLL = 0.5


@dataclass(frozen=True)
class _VoiceTranscriptCheckpoint:
    path: Path
    offset: int
    backend: str


def _voice_transcript_checkpoint(wid: str) -> _VoiceTranscriptCheckpoint | None:
    """Snapshot the authoritative transcript position before a voice send."""
    state = session_manager.window_states.get(wid)
    if not isinstance(state, WindowState) or not state.session_id:
        return None
    path: Path | None = Path(state.transcript_path) if state.transcript_path else None
    if path is None or not path.is_file():
        if state.backend == "codex":
            from ..codex_session_io import build_session_file_path
        else:
            from ..session_claude_io import build_session_file_path

        path = build_session_file_path(state.session_id, state.cwd)
    if path is None or not path.is_file():
        return None
    try:
        offset = path.stat().st_size
    except OSError:
        return None
    return _VoiceTranscriptCheckpoint(path=path, offset=offset, backend=state.backend)


def _transcript_contains_voice_text(
    checkpoint: _VoiceTranscriptCheckpoint, text: str
) -> bool:
    """Check only rows appended after ``checkpoint`` for the exact user text."""
    try:
        size = checkpoint.path.stat().st_size
        start = checkpoint.offset if size >= checkpoint.offset else 0
        with checkpoint.path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read()
    except OSError:
        return False
    expected = text.strip()
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        candidate = ""
        if checkpoint.backend == "codex":
            payload = row.get("payload")
            if isinstance(payload, dict):
                if (
                    row.get("type") == "event_msg"
                    and payload.get("type") == "user_message"
                ):
                    candidate = str(payload.get("message") or "")
                elif (
                    row.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    content = payload.get("content", "")
                    if isinstance(content, list):
                        candidate = "\n".join(
                            str(item.get("text") or "")
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") in ("input_text", "text")
                        )
                    elif isinstance(content, str):
                        candidate = content
        elif row.get("type") == "user":
            message = row.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, list):
                    from ..transcript_parser import TranscriptParser

                    candidate = TranscriptParser.extract_text_only(content)
                elif isinstance(content, str):
                    candidate = content
        if candidate.strip() == expected:
            return True
    return False


async def _wait_for_voice_transcript(
    checkpoint: _VoiceTranscriptCheckpoint | None,
    text: str,
    *,
    wid: str | None = None,
) -> bool | None:
    """Wait for exact delivery proof in the target session transcript.

    A fresh Codex session has no rollout/session_map binding before its first
    accepted prompt. In that case keep polling the binding and scan the new
    transcript from byte zero instead of treating "no checkpoint" as success.
    """
    if checkpoint is None and wid is None:
        return None
    if checkpoint is None and wid is not None:
        provisional = session_manager.window_states.get(wid)
        if not isinstance(provisional, WindowState):
            # A real fresh-session flow always publishes provisional state
            # before exposing the card. Missing state means this is a legacy
            # caller (or a focused unit-test double), so transcript proof is
            # not available on this path.
            return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _VOICE_TRANSCRIPT_CONFIRM_TIMEOUT
    while True:
        if checkpoint is None and wid is not None:
            await session_manager.load_session_map()
            state = session_manager.window_states.get(wid)
            if isinstance(state, WindowState) and state.session_id:
                path = Path(state.transcript_path) if state.transcript_path else None
                if path is None or not path.is_file():
                    if state.backend == "codex":
                        from ..codex_session_io import build_session_file_path
                    else:
                        from ..session_claude_io import build_session_file_path
                    path = build_session_file_path(state.session_id, state.cwd)
                if path is not None and path.is_file():
                    checkpoint = _VoiceTranscriptCheckpoint(
                        path=path, offset=0, backend=state.backend
                    )
        if checkpoint is not None and await asyncio.to_thread(
            _transcript_contains_voice_text, checkpoint, text
        ):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_VOICE_TRANSCRIPT_CONFIRM_POLL, remaining))


async def _send_with_delivery_proof(
    wid: str, text: str, sess: Session | None
) -> tuple[bool, str]:
    """Send one prompt and require an exact Codex transcript acknowledgement."""
    transcript_checkpoint = _voice_transcript_checkpoint(wid)
    message = ""
    for attempt in range(1, 3):
        success, message = await session_manager.send_to_window(wid, text)
        if not success:
            continue
        if message.startswith("Queued for "):
            return True, message
        if sess is None or sess.backend != "codex":
            return True, message
        if not await tmux_manager.ensure_codex_prompt_submitted(wid, text):
            message = "Codex kept the text in its input field"
            continue
        # TUI slash commands do not become ordinary user_message rows.
        if text.lstrip().startswith("/"):
            return True, message
        confirmed = await _wait_for_voice_transcript(
            transcript_checkpoint, text, wid=wid
        )
        if confirmed is True or confirmed is None:
            return True, message
        logger.warning(
            "Codex delivery absent from transcript; retrying exact prompt "
            "window=%s attempt=%d/2 text_len=%d",
            wid,
            attempt,
            len(text),
        )
        message = "Prompt did not appear in the Codex transcript"
    return False, message or "Delivery was not acknowledged"


def _enqueue_voice(
    user_id: int, wid: str
) -> tuple[asyncio.Future[bool] | None, asyncio.Future[bool]]:
    key = (user_id, wid)
    previous = _voice_barriers.get(key)
    current = asyncio.get_running_loop().create_future()
    _voice_barriers[key] = current
    return previous, current


async def _wait_for_voice(barrier: asyncio.Future[bool]) -> bool:
    _voice_waiters[barrier] = _voice_waiters.get(barrier, 0) + 1
    try:
        return await asyncio.shield(barrier)
    finally:
        remaining = _voice_waiters.get(barrier, 1) - 1
        if remaining > 0:
            _voice_waiters[barrier] = remaining
        else:
            _voice_waiters.pop(barrier, None)


async def _await_prior_voice(user_id: int, wid: str) -> bool:
    barrier = _voice_barriers.get((user_id, wid))
    if barrier is None:
        return True
    return await _wait_for_voice(barrier)


def _release_voice(
    user_id: int, wid: str, barrier: asyncio.Future[bool], *, delivered: bool
) -> None:
    key = (user_id, wid)
    if not barrier.done():
        barrier.set_result(delivered)
    if _voice_barriers.get(key) is barrier:
        _voice_barriers.pop(key, None)


def _append_dropped_queue_notice(
    user_id: int, text: str, barrier: asyncio.Future[bool] | None
) -> str:
    if barrier is None or _voice_waiters.get(barrier, 0) == 0:
        return text
    return f"{text}\n\n{t(user_id, 'voice.queued_dropped')}"


async def _download_voice_bytes(voice: Any, *, user_id: int, wid: str) -> bytes:
    """Fetch a Telegram voice payload, retrying transient network failures."""
    for attempt in range(1, _VOICE_DOWNLOAD_ATTEMPTS + 1):
        stage = "get_file"
        try:
            voice_file = await voice.get_file()
            stage = "download"
            return bytes(await voice_file.download_as_bytearray())
        except NetworkError as e:
            logger.warning(
                "Voice download network failure user=%d window=%s "
                "stage=%s attempt=%d/%d: %s",
                user_id,
                wid,
                stage,
                attempt,
                _VOICE_DOWNLOAD_ATTEMPTS,
                e,
            )
            if attempt >= _VOICE_DOWNLOAD_ATTEMPTS:
                raise
            await asyncio.sleep(_VOICE_DOWNLOAD_RETRY_DELAYS[attempt - 1])

    raise RuntimeError("unreachable")


# Telegram's Bot API caps file *downloads* (getFile) at 20 MB. A larger
# upload surfaces here as BadRequest("file is too big") on .get_file();
# turn that into actionable copy instead of a silent ERROR in the logs.
_FILE_TOO_BIG_MSG = (
    "❌ Telegram won't let me download this file — it's over 20 MB.\n\n"
    "This is a Telegram **Bot API** limit (bots can only fetch files up to "
    "20 MB via getFile), not a ccbot setting. Ways around it:\n"
    "• gzip or split the file under 20 MB and resend\n"
    "• drop it straight into the session's `.ccbot-inbox/` folder — no "
    "Telegram round-trip, no limit\n"
    "• bypass with your own Telegram **user session** (MTProto / user-api, "
    "e.g. Telethon or Pyrogram): a user account downloads up to 2 GB (4 GB "
    "with Premium). That needs a user-api fetch path wired into ccbot."
)


def _is_file_too_big(err: BadRequest) -> bool:
    """True when a getFile call hit Telegram's 20 MB Bot-API download cap."""
    return "too big" in str(err).lower()


class _RepostHandle:
    """Mutable flag used with :func:`_card_repost_bracket`. Call
    :meth:`commit` after the pane send succeeded; the bracket then
    reposts the live card on exit.
    """

    __slots__ = ("do_repost",)

    def __init__(self) -> None:
        self.do_repost = False

    def commit(self) -> None:
        self.do_repost = True


@asynccontextmanager
async def _card_repost_bracket(
    bot: Bot, user_id: int, sess: Session | None
) -> AsyncGenerator[_RepostHandle, None]:
    """Bracket a send-to-pane operation with the live-card repost machinery.

    Entry: drop any Menu/sub-screen pause + arm ``repost_intent`` so a
    concurrent ``update_session_card`` buffers events instead of spawning
    a second card above the user's message.
    Exit (only when caller invoked ``handle.commit()``): repost the card
    below the user's message and drain buffered events into it.
    Always: clear ``repost_intent`` so the live card unblocks for the
    next turn.

    No-op when ``sess`` is None (orphan window / no Session record).
    """
    handle = _RepostHandle()
    if sess is None or not is_active_for_user(user_id, sess):
        yield handle
        return
    await resume_card_view(bot, user_id, sess)
    begin_repost_intent(user_id, sess.id)
    try:
        yield handle
    finally:
        if handle.do_repost and is_active_for_user(user_id, sess):
            try:
                get_card_state(user_id, sess).turn_phase = TurnPhase.RUNNING
                await repost_card(bot, user_id, sess)
            except Exception as e:
                logger.debug("repost_card failed: %s", e)
        end_repost_intent(user_id, sess.id)


async def _pane_has_interactive_ui(wid: str) -> bool:
    """True iff the window's pane is currently showing an interactive prompt.

    Cheap capture-and-classify used by the voice path to verify delivery —
    a transcription typed into a pane that is showing a Yes/No prompt gets
    consumed as menu navigation and lost, so the caller needs to know.
    """
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        return False
    pane_text = await tmux_manager.capture_pane(w.window_id)
    return bool(pane_text) and is_interactive_ui(pane_text)


async def _intercept_if_pending_ui(
    bot: Bot,
    user_id: int,
    wid: str,
    reply_to: Any,
    wasnt_sent_notice: str | None = None,
) -> bool:
    """If the pane has a pending interactive UI, surface it and intercept.

    Returns True iff the caller MUST NOT call ``send_to_window``: the
    AskUserQuestion / ExitPlanMode / Permission prompt on the pane would
    otherwise consume the user's text as menu keystrokes (digits select
    options, Enter submits). Caller should ``return`` on True.

    ``wasnt_sent_notice`` overrides the "your message wasn't sent" reply —
    the voice path passes a resend-oriented line since a transcription, unlike
    typed text, can't just be retyped.

    Surface preference:
      - Active session (sess matches ``get_active_session``) → kb-mode
        card via ``enter_kb_mode``. Idempotent: a no-op if the card is
        already in kb-mode for the same prompt.
      - Orphan window or bg session → legacy floating msg via
        ``handle_interactive_ui``.
    """
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        return False
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text or not is_interactive_ui(pane_text):
        return False
    sess = session_manager.find_session_by_window(wid)
    active = session_manager.get_active_session(user_id)
    is_active = sess is not None and active is not None and active.id == sess.id
    surfaced = False
    if is_active and sess is not None:
        content_obj = extract_interactive_content(pane_text)
        if content_obj is not None:
            await enter_kb_mode(
                bot, user_id, sess, content_obj.content, content_obj.name
            )
            surfaced = True
    if not surfaced:
        await handle_interactive_ui(bot, user_id, wid)
    logger.info(
        "intercepted_user_msg_pending_ui user=%d wid=%s",
        user_id,
        wid,
        extra={
            "event": "intercepted_user_msg_pending_ui",
            "user_id": user_id,
            "window_id": wid,
        },
    )
    try:
        await safe_reply(
            reply_to,
            wasnt_sent_notice
            or (
                "⏳ Pending prompt above — answer it via the keyboard first. "
                "Your message wasn't sent."
            ),
        )
    except Exception:
        pass
    return True


# --- forward_command — any /command that has no dedicated handler goes here ---


async def forward_command_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
) -> bool:
    """Forward an unhandled /command as a slash to the active Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return False
    if not update.message:
        return False

    cmd_text = update.message.text or ""
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = pinned_wid or active_window(user.id)
    if not wid:
        await safe_reply(
            update.message, "❌ No active session. Use /new to create one."
        )
        return False
    if pinned_wid is None and not await _await_prior_voice(user.id, wid):
        return False

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return False

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await fire_typing(context.bot, user.id, "forward_command", window_id=wid)
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return False
    sess = session_manager.find_session_by_window(wid)
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _send_with_delivery_proof(wid, cc_slash, sess)
        if success:
            # /clear: drop the session association so we re-detect once a
            # new session id is written by the next user message.
            if cc_slash.strip().lower() == "/clear":
                logger.info("Clearing session for window %s after /clear", display)
                session_manager.clear_window_session(wid)
                if sess is not None:
                    await clear_card(context.bot, user.id, sess)
                    await resume_card_view(context.bot, user.id, sess)
                await safe_reply(
                    update.message,
                    "🧹 Context cleared. Next message starts a fresh Claude session.",
                )
            else:
                repost.commit()
        else:
            await safe_reply(update.message, f"❌ {message}")
            return False
    return True
