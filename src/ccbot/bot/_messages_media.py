"""Forwarded-content, photo and document handler implementation.

Public imports remain in :mod:`ccbot.bot.messages`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

from telegram import Bot, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..handlers.message_sender import (
    safe_reply,
)
from ..handlers.typing import fire_typing
from ..handlers.inbox import save_inbox_file
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..utils import ccbot_dir
from ._common import active_window, is_user_allowed


__all__ = [
    "_forward_attribution",
    "_hidden_link_urls",
    "unsupported_content_handler",
    "_forward_inbox_file",
    "photo_handler",
    "document_handler",
]

if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    _FILE_TOO_BIG_MSG = cast(Any, None)
    _await_prior_voice = cast(Any, None)
    _card_repost_bracket = cast(Any, None)
    _intercept_if_pending_ui = cast(Any, None)
    _is_file_too_big = cast(Any, None)
    _send_with_delivery_proof = cast(Any, None)

logger = logging.getLogger(__name__)


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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
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
    wid_for_queue = pinned_wid or active_window(user.id)
    if wid_for_queue is not None:
        if pinned_wid is None and not await _await_prior_voice(user.id, wid_for_queue):
            return False

    caption = (msg.caption or "").strip()
    if caption:
        wid = pinned_wid or active_window(user.id)
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


async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
) -> bool:
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

    wid = pinned_wid or active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return False
    if pinned_wid is None and not await _await_prior_voice(user.id, wid):
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


async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
) -> bool:
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

    wid = pinned_wid or active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return False
    if pinned_wid is None and not await _await_prior_voice(user.id, wid):
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
