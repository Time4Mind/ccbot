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
from pathlib import Path
from typing import Any

from telegram import Bot, Update
from telegram.constants import ChatAction
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
)
from ..handlers.interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from ..handlers.message_queue import clear_status_msg_info
from ..handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_reply,
    send_with_fallback,
)
from ..handlers.notifications import lookup_session_for_message
from ..handlers.inbox import save_inbox_file
from ..markdown_v2 import convert_markdown
from ..naming import maybe_auto_name
from ..session import session_manager
from ..terminal_parser import extract_bash_output, is_interactive_ui
from ..tmux_manager import tmux_manager
from ..transcribe import transcribe_voice
from ..utils import ccbot_dir
from ._common import active_window, is_user_allowed

logger = logging.getLogger(__name__)


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
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        # /clear: drop the session association so we re-detect once a new
        # session id is written by the next user message.
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

        await msg.chat.send_action(ChatAction.TYPING)
        clear_status_msg_info(user.id, wid)
        success, message = await session_manager.send_to_window(wid, text_to_send)
        if not success:
            await safe_reply(msg, f"❌ {message}")
            return
        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.touch_session(sess.id)
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
    """Send a synthetic 'received file' notice to the active session."""
    rel = file_path.name
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess else ""
    location = f"{workdir}/.ccbot-inbox/{rel}" if workdir else str(file_path)
    text_to_send = (
        f"{caption}\n\n({label} attached: {location})"
        if caption
        else f"({label} attached: {location})"
    )
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass
    clear_status_msg_info(user_id, wid)
    return await session_manager.send_to_window(wid, text_to_send)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the user's photo into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
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
    tg_file = await photo.get_file()
    filename = f"{photo.file_unique_id}.jpg"

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    success, message = await _forward_inbox_file(
        user.id, wid, user.id, file_path, caption, "image", context.bot
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    # No success reply — the user just sent the image, they don't need
    # the bot to tell them it was received. Errors still surface above.


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the user's document into the active session's inbox + notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
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
    tg_file = await doc.get_file()

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    success, message = await _forward_inbox_file(
        user.id, wid, user.id, file_path, caption, "document", context.bot
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    # No success reply — see photo_handler for the same reasoning.


# --- voice ---


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe the voice and forward as text to the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if config.voice_backend == "off":
        await safe_reply(update.message, "⚠ Voice is disabled (VOICE_BACKEND=off).")
        return
    if config.voice_backend == "openai" and not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ VOICE_BACKEND=openai but OPENAI_API_KEY is unset.\n"
            "Set the key or switch VOICE_BACKEND to whisper/auto.",
        )
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

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, wid)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, f'🎤 "{text}"')


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
            else:
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


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
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

    # Typing a message exits any open Menu sub-screen — drop the markers
    # so the next switcher tap / history paginate doesn't think the user
    # is still on /list or /history.
    from .callbacks.more_menu import clear_view_markers as _clear_view_markers

    _clear_view_markers(context.user_data)

    # Reply-quote routing: if the user replied to a bot message that
    # belongs to a non-active session, send this single message there
    # without changing the active session pointer.
    reply = update.message.reply_to_message
    if reply is not None:
        target_sid = lookup_session_for_message(user.id, reply.message_id)
        if target_sid:
            target = session_manager.get_session(target_sid)
            active_sess = session_manager.get_active_session(user.id)
            same_as_active = active_sess is not None and active_sess.id == target_sid
            if (
                target is not None
                and target.window_id
                and target.state in ("active", "idle")
                and not same_as_active
            ):
                tw = await tmux_manager.find_window_by_id(target.window_id)
                if tw:
                    ok, sm = await session_manager.send_to_window(
                        target.window_id, text
                    )
                    if ok:
                        session_manager.touch_session(target.id)
                        await safe_reply(
                            update.message,
                            f"→ \\[{target.name or target.id}\\]",
                        )
                        return
                    await safe_reply(update.message, f"❌ {sm}")
                    return

    wid = active_window(user.id)
    if wid is None:
        # No active session — start a directory browser to create one.
        # The pending text is held in user_data and forwarded after creation.
        logger.info("No active session: showing directory browser (user=%d)", user.id)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["_pending_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info("Stale active session: window %s gone (user=%d)", display, user.id)
        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.mark_session_lost(sess.id)
        await clear_session_state(user.id, wid, context.bot)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # New message pushes pane content down — kill any in-flight bash capture.
    cancel_bash_capture(user.id, wid)

    # Catch interactive UIs that polling might have missed before sending.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if pane_text and is_interactive_ui(pane_text):
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, window=%s)",
            user.id,
            wid,
        )
        await handle_interactive_ui(context.bot, user.id, wid)
        await asyncio.sleep(0.3)

    import time as _time

    from .. import metrics
    from ..handlers.notifications import resume_card_view

    # If the user typed while looking at a Menu / sub-screen on this
    # session's card, drop the pause so incoming events render again.
    sess = session_manager.find_session_by_window(wid)
    if sess is not None:
        await resume_card_view(context.bot, user.id, sess)

    _t0 = _time.time()
    success, message = await session_manager.send_to_window(wid, text)
    metrics.observe("tg_to_claude_latency_ms", (_time.time() - _t0) * 1000.0)
    metrics.inc("tg_messages_in")
    if not success:
        metrics.inc("tg_send_failures")
        await safe_reply(update.message, f"❌ {message}")
        return

    sess = session_manager.find_session_by_window(wid)
    if sess is not None:
        session_manager.touch_session(sess.id)
        looks_default = (not sess.name) or sess.name.startswith("session-")
        if looks_default and len(text) >= 50:
            asyncio.create_task(maybe_auto_name(sess.id, text))

    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, wid)] = task

    interactive_window = get_interactive_window(user.id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid)

    # User-message disposition: keep the live card visually below the
    # user's latest line per the active card_position setting.
    pos_mode = session_manager.get_user_settings(user.id).get("card_position", "push")
    if pos_mode == "delete":
        try:
            await context.bot.delete_message(
                chat_id=user.id, message_id=update.message.message_id
            )
        except Exception as e:
            logger.debug("card_position=delete user msg delete failed: %s", e)
    elif pos_mode == "repost" and sess is not None:
        from ..handlers.notifications import repost_card

        try:
            await repost_card(context.bot, user.id, sess)
        except Exception as e:
            logger.debug("card_position=repost failed: %s", e)


# Re-export so existing callers (callbacks/dir_browser.py) keep working.
from ._session_create import create_and_activate_session  # noqa: E402

__all__ = ["create_and_activate_session"]
