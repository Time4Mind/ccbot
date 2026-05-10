"""Shared helpers used across the bot package — auth, lookups, common UX bits.

Anything in this module is tiny, side-effect-free, and may be imported by
any other ``bot/*`` module without creating a cycle. UI-rendering helpers
that touch keyboards live here too because they're shared between command
and callback handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram import Bot

from ..config import config
from ..handlers.menu import build_footer_keyboard
from ..handlers.message_sender import safe_send
from ..handlers.switcher import build_session_preview
from ..session import Session, session_manager

logger = logging.getLogger(__name__)


# Claude Code commands shown in bot menu (forwarded via tmux). Order
# preserved in setMyCommands. Dropped: /cost (duplicate of /status), /help
# (meta, produces no useful TG output).
CC_COMMANDS: dict[str, str] = {
    "model": "↗ Switch AI model",
    "effort": "↗ Set thinking effort",
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "memory": "↗ Edit CLAUDE.md",
}


def is_user_allowed(user_id: int | None) -> bool:
    """Authorization check — single allowlist line in env."""
    return user_id is not None and config.is_user_allowed(user_id)


def active_window(user_id: int) -> str | None:
    """Return the user's active tmux window_id, or None if none active."""
    return session_manager.get_active_window(user_id)


def shorten_workdir(path: str) -> str:
    """Replace user's home prefix with ~ so paths fit on one row."""
    if not path:
        return "?"
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def resolve_ident(ident: str) -> Session | None:
    """Resolve a /command argument that may be a session id (short hex) or a name.

    Falls back to a case-insensitive name match. Returns None when nothing matches.
    """
    if not ident:
        return None
    sess = session_manager.get_session(ident)
    if sess is not None and sess.state in ("active", "idle", "archived", "completed"):
        return sess
    needle = ident.casefold()
    for s in session_manager.sessions.values():
        if s.name.casefold() == needle:
            return s
    return None


async def render_session_preview(sess: Session) -> str:
    """Build a context-preview text for a Session — header + last user/assistant
    + last tool calls. Read from the session's JSONL transcript.
    """
    last_user = ""
    last_assistant = ""
    last_tools: list[str] = []
    if sess.window_id:
        try:
            messages, _total = await session_manager.get_recent_messages(sess.window_id)
        except Exception as e:
            logger.debug("preview: get_recent_messages failed: %s", e)
            messages = []
        for m in reversed(messages):
            role = m.get("role")
            ctype = m.get("content_type", "text")
            text = m.get("text", "")
            if not text:
                continue
            if role == "user" and not last_user:
                last_user = text
            elif role == "assistant" and ctype == "text" and not last_assistant:
                last_assistant = text
            elif ctype == "tool_use" and len(last_tools) < config.preview_tools:
                first_line = text.splitlines()[0] if text else ""
                last_tools.append(f"[tool] {first_line}")
            if last_user and last_assistant and len(last_tools) >= config.preview_tools:
                break
    return build_session_preview(
        sess,
        last_user=last_user,
        last_assistant=last_assistant,
        last_tools=list(reversed(last_tools)),
    )


def is_media_message(msg: Any) -> bool:
    """True if `msg` is a Telegram message that carries non-text media.

    Photo / document / video / animation messages can't be edited via
    edit_message_text — we have to delete+resend to switch back to a text
    view. Common path when user came from /screenshot.
    """
    if msg is None:
        return False
    return bool(
        getattr(msg, "photo", None)
        or getattr(msg, "document", None)
        or getattr(msg, "video", None)
        or getattr(msg, "animation", None)
    )


async def set_view(
    query: Any,
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: Any | None,
) -> None:
    """Edit the carrier message to (text, reply_markup) — or, if the carrier
    is a photo/document, delete it and send a fresh text message instead.

    Keeps `last_switcher_msg_id` pointing at whatever message ends up
    carrying the inline keyboard.
    """
    if is_media_message(query.message):
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug("set_view: delete media carrier failed: %s", e)
        sent = await safe_send(bot, user_id, text, reply_markup=reply_markup)
        if sent and reply_markup is not None:
            session_manager.set_last_switcher_msg(user_id, sent.message_id)
        return
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.debug("set_view: edit failed: %s", e)
    if query.message and reply_markup is not None:
        session_manager.set_last_switcher_msg(user_id, query.message.message_id)


async def open_more_in_place(query: Any, user_id: int) -> None:
    """Edit the current message into the Menu screen."""
    from ..handlers.menu import render_more_text

    text = render_more_text(user_id)
    keyboard = build_footer_keyboard(user_id, screen="more")
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard)
    except Exception as e:
        logger.debug("open more in place edit failed: %s", e)
    if query.message and keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, query.message.message_id)


__all__ = [
    "CC_COMMANDS",
    "active_window",
    "is_media_message",
    "is_user_allowed",
    "logger",
    "open_more_in_place",
    "render_session_preview",
    "resolve_ident",
    "set_view",
    "shorten_workdir",
]
