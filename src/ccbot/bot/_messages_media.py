"""Forwarded-content, photo and document handler implementation.

Public imports remain in :mod:`ccbot.bot.messages`.
"""

from __future__ import annotations

import logging
import html
import asyncio
import tempfile
from collections.abc import Mapping
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
from ..transfer_runtime import get_node_runtime
from ..utils import ccbot_dir
from ._common import active_window, is_user_allowed
from ._messages_preprocessing import PreparedDispatch, prepare_request_for_dispatch


__all__ = [
    "_forward_attribution",
    "_hidden_link_urls",
    "_incoming_rich_text",
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


def _incoming_rich_text(msg: Any) -> str:
    """Recover Bot API rich-message text unknown to the installed PTB model."""
    api_kwargs = getattr(msg, "api_kwargs", None)
    payload = (
        api_kwargs.get("rich_message") if isinstance(api_kwargs, Mapping) else None
    )
    if not isinstance(payload, Mapping):
        return ""
    markdown = payload.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return html.unescape(markdown).strip()

    def flatten(node: Any) -> list[str]:
        if isinstance(node, str):
            return [html.unescape(node).strip()] if node.strip() else []
        if isinstance(node, list):
            out: list[str] = []
            for item in node:
                out.extend(flatten(item))
            return out
        if not isinstance(node, Mapping):
            return []
        for key in ("text", "title"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return [html.unescape(value).strip()]
            if isinstance(value, (dict, list)):
                nested = flatten(value)
                if nested:
                    return nested
        out = []
        for key in ("blocks", "items", "children", "content", "rows", "cells"):
            out.extend(flatten(node.get(key)))
        return out

    return "\n".join(flatten(payload.get("blocks"))).strip()


def _rich_photo_refs(msg: Any) -> list[tuple[str, str]]:
    """Return one largest downloadable PhotoSize per Rich Message photo block."""
    api_kwargs = getattr(msg, "api_kwargs", None)
    payload = (
        api_kwargs.get("rich_message") if isinstance(api_kwargs, Mapping) else None
    )
    if not isinstance(payload, Mapping):
        return []

    refs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, Mapping):
            return
        if node.get("type") == "photo" and isinstance(node.get("photo"), list):
            sizes = [item for item in node["photo"] if isinstance(item, Mapping)]
            if sizes:
                largest = max(
                    sizes,
                    key=lambda item: (
                        int(item.get("width") or 0) * int(item.get("height") or 0),
                        int(item.get("file_size") or 0),
                    ),
                )
                file_id = str(largest.get("file_id") or "")
                unique_id = str(largest.get("file_unique_id") or file_id)
                if file_id and file_id not in seen:
                    seen.add(file_id)
                    refs.append((file_id, unique_id))
        for value in node.values():
            if isinstance(value, (Mapping, list)):
                walk(value)

    walk(payload.get("blocks"))
    return refs


async def _save_session_inbox_file(
    sess: Any,
    workdir: str,
    filename: str,
    fetch: Any,
) -> str:
    """Store locally or atomically upload to the owning worker."""
    if sess is None or getattr(sess, "node_id", "local") == "local":
        return str(await save_inbox_file(workdir, filename, fetch))
    node_id = str(sess.node_id)
    routing_id = str(
        getattr(sess, "worker_session_id", "") or getattr(sess, "claude_session_id", "")
    )
    runtime = get_node_runtime(node_id)
    if runtime is None or not routing_id:
        raise RuntimeError("Remote node session is not available")
    with tempfile.TemporaryDirectory(prefix="ccbot-inbox-") as staging:
        staged = await save_inbox_file(staging, filename, fetch)
        content = await asyncio.to_thread(staged.read_bytes)
        result = await runtime.upload_inbox_file(node_id, routing_id, filename, content)
    relative = str(result.get("relative_path", ""))
    if not relative.startswith(".ccbot-inbox/") or Path(
        relative
    ).name != relative.removeprefix(".ccbot-inbox/"):
        raise RuntimeError("Worker returned an invalid inbox path")
    return relative


async def _save_rich_photos(msg: Any, bot: Bot, sess: Any, workdir: str) -> list[str]:
    saved: list[str] = []
    for file_id, unique_id in _rich_photo_refs(msg):
        tg_file = await bot.get_file(file_id)

        async def fetch(target: Path, source: Any = tg_file) -> None:
            await source.download_to_drive(target)

        saved.append(
            await _save_session_inbox_file(sess, workdir, f"{unique_id}.jpg", fetch)
        )
    return saved


async def _media_session(wid: str) -> Any | None:
    """Resolve ownership before any local tmux liveness check."""
    sess = session_manager.find_session_by_window(wid)
    if sess is not None and getattr(sess, "node_id", "local") != "local":
        routing_id = getattr(sess, "worker_session_id", "") or getattr(
            sess, "claude_session_id", ""
        )
        return sess if routing_id and get_node_runtime(sess.node_id) else None
    return sess if await tmux_manager.find_window_by_id(wid) else None


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

    When the message carries text or downloadable Rich Message photos,
    preserve both in one request to the active session. Other unsupported
    media still contributes its caption and hidden ``text_link`` URLs.

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
    if not caption:
        caption = _incoming_rich_text(msg)
    rich_photo_refs = _rich_photo_refs(msg)
    if caption or rich_photo_refs:
        wid = pinned_wid or active_window(user.id)
        if wid is None:
            await safe_reply(
                msg,
                "❌ No active session. Send a text message first or use /new.",
            )
            return False
        sess = await _media_session(wid)
        if sess is None:
            display = session_manager.get_display_name(wid)
            await safe_reply(
                msg,
                f"❌ Window '{display}' no longer exists.\n"
                "Send a message to start a new session.",
            )
            return False

        prepared_dispatch = (
            await prepare_request_for_dispatch(
                update,
                context,
                user.id,
                wid,
                caption,
                input_kind="text",
            )
            if caption
            else PreparedDispatch(text="")
        )
        caption = prepared_dispatch.text
        prefix = _forward_attribution(msg)
        hidden_urls = _hidden_link_urls(msg)
        body_parts = [prefix + caption] if prefix else ([caption] if caption else [])
        if hidden_urls:
            body_parts.append("Links:")
            body_parts.extend(hidden_urls)
        session_workdir = str(getattr(sess, "workdir", "") or "")
        workdir = session_workdir or str(ccbot_dir() / "images")
        try:
            saved_photos = await _save_rich_photos(msg, context.bot, sess, workdir)
        except BadRequest as exc:
            if _is_file_too_big(exc):
                await safe_reply(msg, _FILE_TOO_BIG_MSG)
                return False
            raise
        except Exception as exc:
            logger.warning("Rich media delivery failed window=%s: %s", wid, exc)
            await safe_reply(msg, f"❌ File was not delivered: {exc}")
            return False
        for file_path in saved_photos:
            file_ref = str(file_path)
            body_parts.append(
                file_ref
                if file_ref.startswith(".ccbot-inbox/") or not session_workdir
                else f".ccbot-inbox/{Path(file_ref).name}"
            )
        text_to_send = "\n".join(body_parts)

        await fire_typing(context.bot, user.id, "caption_forward", window_id=wid)
        if await _intercept_if_pending_ui(
            context.bot,
            user.id,
            wid,
            msg,
            wait_until_clear=pinned_wid is not None,
        ):
            return False
        async with _card_repost_bracket(context.bot, user.id, sess) as repost:
            success, message = await _send_with_delivery_proof(wid, text_to_send, sess)
            if not success:
                await safe_reply(msg, f"❌ {message}")
                return False
            if sess is not None:
                session_manager.touch_session(sess.id)
            prepared_dispatch.confirm_delivery()
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

    sess = await _media_session(wid)
    if sess is None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return False

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

    try:
        file_path = await _save_session_inbox_file(sess, workdir, filename, _fetch)
    except Exception as exc:
        logger.warning("Photo delivery failed window=%s: %s", wid, exc)
        await safe_reply(update.message, f"❌ File was not delivered: {exc}")
        return False

    caption = update.message.caption or ""
    prepared_dispatch = (
        await prepare_request_for_dispatch(
            update,
            context,
            user.id,
            wid,
            caption,
            input_kind="text",
        )
        if caption.strip()
        else PreparedDispatch(text="")
    )
    caption = prepared_dispatch.text
    if await _intercept_if_pending_ui(
        context.bot,
        user.id,
        wid,
        update.message,
        wait_until_clear=pinned_wid is not None,
    ):
        return False
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, Path(file_path), caption, "image", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return False
        prepared_dispatch.confirm_delivery()
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

    sess = await _media_session(wid)
    if sess is None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return False

    doc = update.message.document
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

    try:
        file_path = await _save_session_inbox_file(sess, workdir, filename, _fetch)
    except Exception as exc:
        logger.warning("Document delivery failed window=%s: %s", wid, exc)
        await safe_reply(update.message, f"❌ File was not delivered: {exc}")
        return False

    caption = update.message.caption or ""
    prepared_dispatch = (
        await prepare_request_for_dispatch(
            update,
            context,
            user.id,
            wid,
            caption,
            input_kind="text",
        )
        if caption.strip()
        else PreparedDispatch(text="")
    )
    caption = prepared_dispatch.text
    if await _intercept_if_pending_ui(
        context.bot,
        user.id,
        wid,
        update.message,
        wait_until_clear=pinned_wid is not None,
    ):
        return False
    async with _card_repost_bracket(context.bot, user.id, sess) as repost:
        success, message = await _forward_inbox_file(
            user.id, wid, user.id, Path(file_path), caption, "document", context.bot
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return False
        prepared_dispatch.confirm_delivery()
        repost.commit()
    return True
