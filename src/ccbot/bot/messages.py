"""Inbound message handlers — text, voice, photo, document, and the
forward-as-slash-command catch-all.

Also home to:
  - ``create_and_activate_session``: tmux window creation flow shared by
    the directory browser and session picker callback paths.
  - background ``_capture_bash_output`` task driving ``!cmd`` echo from
    the active pane back into chat.
  - the ``forward_command_handler`` that pipes any unhandled /command
    straight into the active session's tmux input.
"""

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

from ..handlers.cleanup import clear_session_state
from ..handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    build_directory_browser,
)
from ..handlers.interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from ..handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_reply,
    send_with_fallback,
    try_rich_edit,
)
from ..handlers.notifications import (
    begin_repost_intent,
    card_is_below,
    clear_card,
    end_repost_intent,
    enter_kb_mode,
    get_card_state,
    is_active_for_user,
    lookup_session_for_message,
    refresh_panel,
    repost_card,
    resume_card_view,
)
from ..handlers.typing import fire_typing
from ..i18n import t
from ..session_models import Session, WindowState
from ..handlers.inbox import save_inbox_file
from ..markdown_v2 import convert_markdown
from ..naming import maybe_auto_name
from ..session import session_manager
from ..terminal_parser import (
    extract_bash_output,
    extract_interactive_content,
    is_interactive_ui,
)
from ..tmux_manager import tmux_manager
from ..transcribe import resolve_voice_backend, transcribe_voice
from ..utils import ccbot_dir
from ._common import active_window, is_user_allowed
from .commands.auth import maybe_consume_code

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
            if (
                row.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "user_message"
            ):
                candidate = str(payload.get("message") or "")
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
    if sess is None:
        yield handle
        return
    await resume_card_view(bot, user_id, sess)
    begin_repost_intent(user_id, sess.id)
    try:
        yield handle
    finally:
        if handle.do_repost:
            try:
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
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Forward an unhandled /command as a slash to the active Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return False
    if not update.message:
        return False

    cmd_text = update.message.text or ""
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = active_window(user.id)
    if not wid:
        await safe_reply(
            update.message, "❌ No active session. Use /new to create one."
        )
        return False
    if not await _await_prior_voice(user.id, wid):
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


# --- non-text catch-all ---


def _forward_attribution(msg: Any) -> str:
    """Return ``[forwarded from @name]\n`` prefix when the message looks
    like a Telegram forward. Best-effort across PTB versions:
    ``forward_origin`` (PTB ≥ 21) and the legacy ``forward_from_chat`` /
    ``forward_from`` fields. Empty string when the message isn't a
    forward at all."""
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        chat = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        if chat is not None:
            handle = (
                getattr(chat, "username", None)
                or getattr(chat, "title", None)
                or "channel"
            )
            return f"[forwarded from @{handle}]\n"
        usr = getattr(fo, "sender_user", None)
        if usr is not None:
            handle = (
                getattr(usr, "username", None)
                or getattr(usr, "first_name", None)
                or "user"
            )
            return f"[forwarded from @{handle}]\n"
        name = getattr(fo, "sender_user_name", None)
        if name:
            return f"[forwarded from {name}]\n"
        return "[forwarded]\n"
    chat = getattr(msg, "forward_from_chat", None)
    if chat is not None:
        handle = (
            getattr(chat, "username", None) or getattr(chat, "title", None) or "channel"
        )
        return f"[forwarded from @{handle}]\n"
    usr = getattr(msg, "forward_from", None)
    if usr is not None:
        handle = (
            getattr(usr, "username", None) or getattr(usr, "first_name", None) or "user"
        )
        return f"[forwarded from @{handle}]\n"
    return ""


def _hidden_link_urls(msg: Any) -> list[str]:
    """Pull URLs out of ``text_link`` entities (anchor-text links whose
    actual URL isn't in the visible body). Plain-text URLs are already
    in the caption text so we don't duplicate them. Operates on both
    ``entities`` (text messages) and ``caption_entities`` (media)."""
    out: list[str] = []
    seen: set[str] = set()
    sources = []
    if getattr(msg, "caption_entities", None):
        sources.append(msg.caption_entities)
    if getattr(msg, "entities", None):
        sources.append(msg.entities)
    for ents in sources:
        for ent in ents:
            etype = getattr(ent, "type", "")
            url = getattr(ent, "url", "") or ""
            if etype == "text_link" and url and url not in seen:
                out.append(url)
                seen.add(url)
    return out


async def unsupported_content_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Catch-all for messages without a dedicated handler.

    When the message carries a caption (typical for forwarded channel
    posts that bundle a video + body text), extract the caption + any
    hidden ``text_link`` URLs and forward the resulting text to the
    active session — the media itself is dropped on the floor since
    Claude can't consume it directly, but the body keeps the context.

    Falls back to the legacy "unsupported" reply when there's no
    caption to salvage.
    """
    if not update.message:
        return False
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return False
    msg = update.message
    wid_for_queue = active_window(user.id)
    if wid_for_queue is not None:
        if not await _await_prior_voice(user.id, wid_for_queue):
            return False

    caption = (msg.caption or "").strip()
    if caption:
        wid = active_window(user.id)
        if wid is None:
            await safe_reply(
                msg,
                "❌ No active session. Send a text message first or use /new.",
            )
            return False
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            await safe_reply(
                msg,
                f"❌ Window '{display}' no longer exists.\n"
                "Send a message to start a new session.",
            )
            return False

        prefix = _forward_attribution(msg)
        hidden_urls = _hidden_link_urls(msg)
        body_parts = [prefix + caption] if prefix else [caption]
        if hidden_urls:
            body_parts.append("Links:")
            body_parts.extend(hidden_urls)
        text_to_send = "\n".join(body_parts)

        await fire_typing(context.bot, user.id, "caption_forward", window_id=wid)
        if await _intercept_if_pending_ui(context.bot, user.id, wid, msg):
            return False
        sess = session_manager.find_session_by_window(wid)
        async with _card_repost_bracket(context.bot, user.id, sess) as repost:
            success, message = await _send_with_delivery_proof(wid, text_to_send, sess)
            if not success:
                await safe_reply(msg, f"❌ {message}")
                return False
            if sess is not None:
                session_manager.touch_session(sess.id)
            repost.commit()
        # No success reply — the user just sent the message; they know
        # they sent it. Errors above still surface.
        return True

    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        msg,
        "⚠ Only text, photo, and voice messages are supported. "
        "Stickers, video, and other media cannot be forwarded to Claude Code.",
    )
    return True


# --- inbox file plumbing (photo + document share this) ---


async def _forward_inbox_file(
    user_id: int,
    wid: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    label: str,
    bot: Bot,
) -> tuple[bool, str]:
    """Route an inbound file to the active session.

    Pane payload is shaped as ``<caption>\\n\\n.ccbot-inbox/<file>`` so
    claude both (a) knows the file exists and where to read it and
    (b) sees whatever instructions the user attached. With no caption
    it's just the relative path on its own line. This is a minimal
    successor to the old verbose ``(image attached: /full/path)``
    synthetic line — short enough not to feel like "the bot speaking
    for the user", complete enough that claude doesn't go blind on a
    silent drop.
    """
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess else ""
    if workdir:
        rel_path = f".ccbot-inbox/{file_path.name}"
    else:
        rel_path = str(file_path)
    text_to_send = f"{caption}\n\n{rel_path}" if caption.strip() else rel_path
    await fire_typing(bot, user_id, "inbox_file_forward", window_id=wid, label=label)
    return await _send_with_delivery_proof(wid, text_to_send, sess)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Drop the user's photo into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return False

    if not update.message or not update.message.photo:
        return False

    wid = active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return False
    if not await _await_prior_voice(user.id, wid):
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

    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")

    photo = update.message.photo[-1]
    try:
        tg_file = await photo.get_file()
    except BadRequest as e:
        if _is_file_too_big(e):
            await safe_reply(update.message, _FILE_TOO_BIG_MSG)
            return False
        raise
    filename = f"{photo.file_unique_id}.jpg"

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return False
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, file_path, caption, "image", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return False
        repost.commit()
    return True


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Drop the user's document into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return False

    if not update.message or not update.message.document:
        return False

    wid = active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return False
    if not await _await_prior_voice(user.id, wid):
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

    doc = update.message.document
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")
    filename = doc.file_name or f"{doc.file_unique_id}.bin"
    try:
        tg_file = await doc.get_file()
    except BadRequest as e:
        if _is_file_too_big(e):
            await safe_reply(update.message, _FILE_TOO_BIG_MSG)
            return False
        raise

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return False
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, file_path, caption, "document", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return False
        repost.commit()
    return True


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


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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

    wid = active_window(user.id)
    if wid is None:
        return await _process_voice(update, context)

    previous, barrier = _enqueue_voice(user.id, wid)
    delivered = False
    try:
        if previous is not None and not await _wait_for_voice(previous):
            return False
        delivered = await _process_voice(
            update, context, pinned_wid=wid, queue_barrier=barrier
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
        try:
            await repost_card(context.bot, user.id, sess)
        except Exception as e:
            logger.debug("voice-pending card repost failed: %s", e)

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


# --- text + bash !cmd capture ---


# Active bash capture tasks: (user_id, window_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}


def cancel_bash_capture(user_id: int, window_id: str) -> None:
    """Cancel any running bash capture for this (user, window) pair."""
    key = (user_id, window_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot, user_id: int, window_id: str, command: str
) -> None:
    """Background task: capture ``!cmd`` output from the pane and surface it.

    Sends the first non-empty capture as a new message, then edits in place
    as more output appears. Stops after 30 ticks (~30 s) or on cancel.
    """
    try:
        await asyncio.sleep(2.0)
        chat_id = user_id
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue
            if output == last_output:
                await asyncio.sleep(1.0)
                continue
            last_output = output

            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                sent = await send_with_fallback(bot, chat_id, output)
                if sent:
                    msg_id = sent.message_id
            # Rich-first so in-place edits keep the same rendering as the
            # initial send (which goes rich via send_with_fallback).
            elif not await try_rich_edit(bot, chat_id, msg_id, output):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, window_id), None)


async def _route_reply_quote(update: Update, user_id: int, text: str) -> bool:
    """Reply-quote routing: if the user replied to a bot message that
    belongs to a non-active session, send this single message there
    without changing the active session pointer.

    Returns True iff the message was fully handled and ``text_handler``
    must ``return`` (sent to the quoted session, send error, or quoted
    message has no session). Returns False to fall through to the
    active-session dispatch — both when there is no reply-quote at all
    and when the quoted session is dead (a warning is emitted first).
    """
    assert update.message is not None
    reply = update.message.reply_to_message
    if reply is None:
        return False
    target_sid = lookup_session_for_message(user_id, reply.message_id)
    if not target_sid:
        return False
    target = session_manager.get_session(target_sid)
    active_sess = session_manager.get_active_session(user_id)
    same_as_active = active_sess is not None and active_sess.id == target_sid
    if (
        target is not None
        and target.window_id
        and target.state in ("active", "idle")
        and not same_as_active
    ):
        tw = await tmux_manager.find_window_by_id(target.window_id)
        if tw:
            ok, sm = await session_manager.send_to_window(target.window_id, text)
            if ok:
                session_manager.touch_session(target.id)
                # Explicit feedback so the user can see which
                # session received the reply-quote — bg session
                # would otherwise stay silent until the next
                # carrier interaction.
                await safe_reply(
                    update.message,
                    f"↩ \\[{target.name or target.id}\\]",
                )
                return True
            await safe_reply(update.message, f"❌ {sm}")
            return True
    elif target is not None and target.state not in ("active", "idle"):
        # User aimed at a dead session (archived/lost/completed).
        # Silent fallback would route to active with no signal —
        # tell them so the routing surprise is visible. Falls
        # through to the active-session dispatch below.
        await safe_reply(
            update.message,
            f"⚠ \\[{target.name or target.id}\\] is {target.state} — "
            "routing to the active session instead.",
        )
    return False


async def _resolve_active_window(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str
) -> str | None:
    """Resolve the active session's tmux window for the inbound text.

    Returns the window id when there is a live active session window.
    Returns None when ``text_handler`` must ``return`` instead — either
    because there is no active session (a directory browser is opened
    with the message queued) or because the active session's
    window is gone (it's marked lost, state cleared, and the user told).
    """
    assert update.message is not None
    wid = active_window(user_id)
    if wid is None:
        # No active session — start a directory browser to create one.
        from ..startup_queue import begin_startup_queue, enqueue_startup_message

        begin_startup_queue(user_id)
        enqueue_startup_message(update, context)
        logger.info("No active session: showing directory browser (user=%d)", user_id)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = await build_directory_browser(
            start_path, user_id=user_id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return None

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info("Stale active session: window %s gone (user=%d)", display, user_id)
        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.mark_session_lost(sess.id)
        await clear_session_state(user_id, wid, context.bot)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return None

    return wid


def _maybe_start_bash_capture(bot: Bot, user_id: int, wid: str, text: str) -> None:
    """Spawn the background ``!cmd`` pane-capture task for a ``!`` prefixed
    message. No-op for normal text. Records the task so a follow-up message
    can cancel it via :func:`cancel_bash_capture`."""
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]
        task = asyncio.create_task(_capture_bash_output(bot, user_id, wid, bash_cmd))
        _bash_capture_tasks[(user_id, wid)] = task


async def _dispatch_text_to_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    wid: str,
    text: str,
) -> bool:
    """Send the user's text to ``wid``'s pane and run the post-send
    bookkeeping under the repost-intent bracket.

    Card handling is gated on the target session still being the user's
    ACTIVE one. A voice message pins its window at receipt, so by the
    time whisper returns the user may well have switched elsewhere — the
    text still goes to the pinned pane (that is the entire point of
    pinning), but the session is a *background* one now, and background
    sessions never post their own chat messages. Doing otherwise dropped
    a bg session's card as the newest message in the chat and handed it
    the live switcher, which is what made a later switcher tap appear to
    edit "the previous message".

    Active path mirrors the original flow: resume the card view + arm
    repost-intent (so concurrent ``update_session_card`` events buffer
    rather than spawning a second card), send the keystrokes, fire the
    early typing indicator, touch + auto-name the session, spawn any
    ``!cmd`` capture, drive a pending interactive UI, and finally put the
    live card below the user's message. The try/finally always clears the
    repost-intent flag even on an early return.
    """
    assert update.message is not None
    import time as _time

    from .. import metrics
    from ..handlers import bg_status

    # If the user typed while looking at a Menu / sub-screen on this
    # session's card, drop the pause so incoming events render again.
    sess = session_manager.find_session_by_window(wid)
    owns_card = sess is not None and is_active_for_user(user_id, sess)
    if owns_card and sess is not None:
        await resume_card_view(context.bot, user_id, sess)
        # Lock spawning out from under us before sending keystrokes —
        # claude can emit the first event of its reply within
        # milliseconds of send_to_window returning, and
        # ``update_session_card`` would otherwise grab the card lock
        # first, see ``state.msg_id is None`` (from the previous turn's
        # ``finalize_task``) and spawn a fresh card just for that event.
        # ``repost_card`` would then spawn a SECOND card and try to
        # delete the first — succeeded delete loses claude's content,
        # failed delete leaves both visible (user-reported "2 от бота
        # после моего сообщения"). The buffer guarantees a single spawn.
        begin_repost_intent(user_id, sess.id)

    # Run the rest of the dispatch under a try/finally that always
    # clears the repost-intent flag — without this, an early return
    # below leaves the flag set forever and the live card stays silent
    # for that session until the bot restarts.
    intent_sess_id = sess.id if (owns_card and sess is not None) else None
    try:
        _t0 = _time.time()
        success, message = await _send_with_delivery_proof(wid, text, sess)
        metrics.observe("tg_to_claude_latency_ms", (_time.time() - _t0) * 1000.0)
        metrics.inc("tg_messages_in")
        if not success:
            metrics.inc("tg_send_failures")
            await safe_reply(update.message, f"❌ Delivery not confirmed: {message}")
            return False

        # Immediate typing-indicator so the user sees feedback within
        # ~500 ms of sending — claude can take 5-30 s before emitting
        # its first event (long tool prelude / thinking) and
        # ``status_polling`` won't fire typing until the pane enters
        # the busy-spinner state. Without this early fire the chat
        # looks frozen. fire_typing throttles to one call per ~4 s
        # per user — if text_handler already fired Typing a moment
        # ago, this is a silent no-op (the indicator is still on).
        if owns_card:
            await fire_typing(
                context.bot, user_id, "text_handler.post_send", window_id=wid
            )

        sess = session_manager.find_session_by_window(wid)
        # ``send_to_window`` and Codex's submit verification can take long
        # enough for the user to switch sessions.  The ``owns_card`` value
        # captured before those awaits is no longer authoritative: using it
        # below would let the old session resume/repost the carrier that the
        # switcher has already handed to the new active session.
        owns_card = sess is not None and is_active_for_user(user_id, sess)
        if sess is not None:
            session_manager.touch_session(sess.id)
            # ``maybe_auto_name`` honours the user's ``haiku_naming``
            # setting and the directory-basename guard internally — we
            # only need to gate the call on a non-trivial seed (Haiku
            # can't summarise "hi" / "ok" into anything useful).
            if len(text) >= 20:
                asyncio.create_task(maybe_auto_name(sess.id, text, user_id))

        _maybe_start_bash_capture(context.bot, user_id, wid, text)

        if owns_card:
            interactive_window = get_interactive_window(user_id)
            if interactive_window and interactive_window == wid:
                await asyncio.sleep(0.2)
                await handle_interactive_ui(context.bot, user_id, wid)

        if sess is None:
            return True

        # Re-check immediately before the card mutation as well.  Auto-name,
        # interactive-UI handling, and other post-send work above may await.
        owns_card = is_active_for_user(user_id, sess)
        if not owns_card:
            # Background session (voice pinned here, user moved on).
            # Its only chat surface is a row in the active card's
            # bg-status panel — no card, no push, no switcher steal.
            if bg_status.update_status(user_id, sess.id, "working"):
                try:
                    await refresh_panel(context.bot, user_id)
                except Exception as e:
                    logger.debug("refresh_panel after bg dispatch failed: %s", e)
            return True

        # Put the live card below the user's message (the card_position
        # setting was ripped out — always-in-front is the single
        # canonical behaviour). Any events claude emitted between
        # send_to_window and here were buffered into state.events by
        # update_session_card (it saw the repost-intent flag and held
        # off rendering); they drain into the card on the next render.
        if card_is_below(user_id, sess.id, update.message.message_id):
            # The card is already in front of this message — the voice
            # flow reposted it at receipt. Repost again and the user
            # gets two cards' worth of churn for one voice; an in-place
            # edit is enough to drain the buffer and drop the pending row.
            try:
                await resume_card_view(context.bot, user_id, sess)
            except Exception as e:
                logger.debug("card repaint failed: %s", e)
        else:
            try:
                await repost_card(context.bot, user_id, sess)
            except Exception as e:
                logger.debug("repost_card failed: %s", e)
        return True
    finally:
        if intent_sess_id is not None:
            end_repost_intent(user_id, intent_sess_id)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return False

    if not update.message or not update.message.text:
        return False

    text = update.message.text
    queued_wid = active_window(user.id)
    if queued_wid is not None:
        if not await _await_prior_voice(user.id, queued_wid):
            return False

    # A pending /login flow owns the next message: it's the OAuth code, not a
    # prompt. Must run before session routing — the code would otherwise be
    # typed into a pane (and echoed into that session's transcript).
    if await maybe_consume_code(update, context):
        return True

    # Ignore text while a picker UI is mid-flight.
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state in (
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_SESSION,
    ):
        await safe_reply(update.message, "Please use the picker above, or tap Cancel.")
        return False

    if await _route_reply_quote(update, user.id, text):
        return True

    wid = await _resolve_active_window(update, context, user.id, text)
    if wid is None:
        return False

    await fire_typing(context.bot, user.id, "text_handler", window_id=wid)

    # New message pushes pane content down — kill any in-flight bash capture.
    cancel_bash_capture(user.id, wid)

    # Pending AskUserQuestion / ExitPlanMode / Permission on the pane
    # would consume our keystrokes as menu navigation (digits select,
    # Enter submits). Surface the prompt to the user and bail before
    # send_to_window — the user must answer via the keyboard.
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return False

    return await _dispatch_text_to_active(update, context, user.id, wid, text)


# Re-export so existing callers (callbacks/dir_browser.py) keep working.
from ._session_create import create_and_activate_session  # noqa: E402

__all__ = ["create_and_activate_session"]
