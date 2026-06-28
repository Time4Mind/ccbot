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
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from telegram import Bot, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..config import config
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
    stash_pending_text,
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
    end_repost_intent,
    enter_kb_mode,
    lookup_session_for_message,
    repost_card,
    resume_card_view,
)
from ..handlers.typing import fire_typing
from ..session_models import Session
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
from ..transcribe import transcribe_voice
from ..utils import ccbot_dir
from ._common import active_window, is_user_allowed

logger = logging.getLogger(__name__)


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


async def _intercept_if_pending_ui(
    bot: Bot,
    user_id: int,
    wid: str,
    reply_to: Any,
) -> bool:
    """If the pane has a pending interactive UI, surface it and intercept.

    Returns True iff the caller MUST NOT call ``send_to_window``: the
    AskUserQuestion / ExitPlanMode / Permission prompt on the pane would
    otherwise consume the user's text as menu keystrokes (digits select
    options, Enter submits). Caller should ``return`` on True.

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
            "⏳ Pending prompt above — answer it via the keyboard first. "
            "Your message wasn't sent.",
        )
    except Exception:
        pass
    return True


# --- forward_command — any /command that has no dedicated handler goes here ---


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward an unhandled /command as a slash to the active Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    cmd_text = update.message.text or ""
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = active_window(user.id)
    if not wid:
        await safe_reply(
            update.message, "❌ No active session. Use /new to create one."
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await fire_typing(context.bot, user.id, "forward_command", window_id=wid)
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return
    sess = session_manager.find_session_by_window(wid)
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await session_manager.send_to_window(wid, cc_slash)
        if success:
            repost.commit()
            # /clear: drop the session association so we re-detect once a
            # new session id is written by the next user message.
            if cc_slash.strip().lower() == "/clear":
                logger.info("Clearing session for window %s after /clear", display)
                session_manager.clear_window_session(wid)
                await safe_reply(
                    update.message,
                    "🧹 Context cleared. Next message starts a fresh Claude session.",
                )
        else:
            await safe_reply(update.message, f"❌ {message}")


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
) -> None:
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
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    msg = update.message

    caption = (msg.caption or "").strip()
    if caption:
        wid = active_window(user.id)
        if wid is None:
            await safe_reply(
                msg,
                "❌ No active session. Send a text message first or use /new.",
            )
            return
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            await safe_reply(
                msg,
                f"❌ Window '{display}' no longer exists.\n"
                "Send a message to start a new session.",
            )
            return

        prefix = _forward_attribution(msg)
        hidden_urls = _hidden_link_urls(msg)
        body_parts = [prefix + caption] if prefix else [caption]
        if hidden_urls:
            body_parts.append("Links:")
            body_parts.extend(hidden_urls)
        text_to_send = "\n".join(body_parts)

        await fire_typing(context.bot, user.id, "caption_forward", window_id=wid)
        if await _intercept_if_pending_ui(context.bot, user.id, wid, msg):
            return
        sess = session_manager.find_session_by_window(wid)
        async with _card_repost_bracket(context.bot, user.id, sess) as repost:
            success, message = await session_manager.send_to_window(wid, text_to_send)
            if not success:
                await safe_reply(msg, f"❌ {message}")
                return
            if sess is not None:
                session_manager.touch_session(sess.id)
            repost.commit()
        # No success reply — the user just sent the message; they know
        # they sent it. Errors above still surface.
        return

    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        msg,
        "⚠ Only text, photo, and voice messages are supported. "
        "Stickers, video, and other media cannot be forwarded to Claude Code.",
    )


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
    return await session_manager.send_to_window(wid, text_to_send)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the user's photo into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return

    if not update.message or not update.message.photo:
        return

    wid = active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")

    photo = update.message.photo[-1]
    try:
        tg_file = await photo.get_file()
    except BadRequest as e:
        if _is_file_too_big(e):
            await safe_reply(update.message, _FILE_TOO_BIG_MSG)
            return
        raise
    filename = f"{photo.file_unique_id}.jpg"

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, file_path, caption, "image", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        repost.commit()


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the user's document into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return

    if not update.message or not update.message.document:
        return

    wid = active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    doc = update.message.document
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")
    filename = doc.file_name or f"{doc.file_unique_id}.bin"
    try:
        tg_file = await doc.get_file()
    except BadRequest as e:
        if _is_file_too_big(e):
            await safe_reply(update.message, _FILE_TOO_BIG_MSG)
            return
        raise

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, file_path, caption, "document", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        repost.commit()


# --- voice ---


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe the voice and forward as text to the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return

    if not update.message or not update.message.voice:
        return

    if config.voice_backend == "off":
        await safe_reply(update.message, "⚠ Voice is disabled (VOICE_BACKEND=off).")
        return
    wid = active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    try:
        text = await transcribe_voice(ogg_data, user_id=user.id)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    await fire_typing(context.bot, user.id, "voice_handler", window_id=wid)

    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return

    sess = session_manager.find_session_by_window(wid)
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await session_manager.send_to_window(wid, text)
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        repost.commit()


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
    with the pending text stashed) or because the active session's
    window is gone (it's marked lost, state cleared, and the user told).
    """
    assert update.message is not None
    wid = active_window(user_id)
    if wid is None:
        # No active session — start a directory browser to create one.
        # The pending text is held in user_data and forwarded after creation.
        logger.info("No active session: showing directory browser (user=%d)", user_id)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, user_id=user_id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            stash_pending_text(context.user_data, text)
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
) -> None:
    """Send the user's text to the active session's pane and run the
    post-send bookkeeping under the repost-intent bracket.

    Mirrors the original inline flow exactly: resume the card view + arm
    repost-intent (so concurrent ``update_session_card`` events buffer
    rather than spawning a second card), send the keystrokes, fire the
    early typing indicator, touch + auto-name the session, spawn any
    ``!cmd`` capture, drive a pending interactive UI, and finally repost
    the live card below the user's message. The try/finally always clears
    the repost-intent flag even on an early return.
    """
    assert update.message is not None
    import time as _time

    from .. import metrics
    from ..handlers.notifications import (
        begin_repost_intent,
        end_repost_intent,
        resume_card_view,
    )

    # If the user typed while looking at a Menu / sub-screen on this
    # session's card, drop the pause so incoming events render again.
    sess = session_manager.find_session_by_window(wid)
    if sess is not None:
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
    intent_sess_id = sess.id if sess is not None else None
    try:
        _t0 = _time.time()
        success, message = await session_manager.send_to_window(wid, text)
        metrics.observe("tg_to_claude_latency_ms", (_time.time() - _t0) * 1000.0)
        metrics.inc("tg_messages_in")
        if not success:
            metrics.inc("tg_send_failures")
            await safe_reply(update.message, f"❌ {message}")
            return

        # Immediate typing-indicator so the user sees feedback within
        # ~500 ms of sending — claude can take 5-30 s before emitting
        # its first event (long tool prelude / thinking) and
        # ``status_polling`` won't fire typing until the pane enters
        # the busy-spinner state. Without this early fire the chat
        # looks frozen. fire_typing throttles to one call per ~4 s
        # per user — if text_handler already fired Typing a moment
        # ago, this is a silent no-op (the indicator is still on).
        await fire_typing(context.bot, user_id, "text_handler.post_send", window_id=wid)

        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.touch_session(sess.id)
            # ``maybe_auto_name`` honours the user's ``haiku_naming``
            # setting and the directory-basename guard internally — we
            # only need to gate the call on a non-trivial seed (Haiku
            # can't summarise "hi" / "ok" into anything useful).
            if len(text) >= 20:
                asyncio.create_task(maybe_auto_name(sess.id, text, user_id))

        _maybe_start_bash_capture(context.bot, user_id, wid, text)

        interactive_window = get_interactive_window(user_id)
        if interactive_window and interactive_window == wid:
            await asyncio.sleep(0.2)
            await handle_interactive_ui(context.bot, user_id, wid)

        # Always repost the live card below the user's message (the
        # card_position setting was ripped out — repost is the single
        # canonical behaviour). Any events claude emitted between
        # send_to_window and here were buffered into state.events by
        # update_session_card (it saw the repost-intent flag and held
        # off rendering); they drain into the freshly-reposted card.
        if sess is not None:
            from ..handlers.notifications import repost_card

            try:
                await repost_card(context.bot, user_id, sess)
            except Exception as e:
                logger.debug("repost_card failed: %s", e)
    finally:
        if intent_sess_id is not None:
            end_repost_intent(user_id, intent_sess_id)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text

    # Ignore text while a picker UI is mid-flight.
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state in (
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_SESSION,
    ):
        await safe_reply(update.message, "Please use the picker above, or tap Cancel.")
        return

    if await _route_reply_quote(update, user.id, text):
        return

    wid = await _resolve_active_window(update, context, user.id, text)
    if wid is None:
        return

    await fire_typing(context.bot, user.id, "text_handler", window_id=wid)

    # New message pushes pane content down — kill any in-flight bash capture.
    cancel_bash_capture(user.id, wid)

    # Pending AskUserQuestion / ExitPlanMode / Permission on the pane
    # would consume our keystrokes as menu navigation (digits select,
    # Enter submits). Surface the prompt to the user and bail before
    # send_to_window — the user must answer via the keyboard.
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return

    await _dispatch_text_to_active(update, context, user.id, wid, text)


# Re-export so existing callers (callbacks/dir_browser.py) keep working.
from ._session_create import create_and_activate_session  # noqa: E402

__all__ = ["create_and_activate_session"]
