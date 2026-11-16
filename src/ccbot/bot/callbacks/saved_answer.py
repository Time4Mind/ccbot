"""Pagination of an immutable historical answer, scoped to its exact message."""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.card_registry import _carrier_edit_lock
from ...handlers.saved_answers import keyboard, read_pages
from ...session import session_manager

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    if not data.startswith("fa:"):
        return False
    from ...keyboard_cleanup import cleanup_queue
    from ...handlers.message_sender import safe_edit

    try:
        _, raw_message, version, raw_page = data.split(":")
        message_id, page = int(raw_message), int(raw_page)
    except (ValueError, TypeError):
        await query.answer()
        return True
    message = query.message
    if (
        message is None
        or message.message_id != message_id
        or message.chat.id != user.id
    ):
        await query.answer()
        return True
    await query.answer()
    async with _carrier_edit_lock(user.id):
        cleanup_queue.load()
        session_id = cleanup_queue.final_sessions.get((user.id, message_id))
        # A carrier handoff may have won after the user tapped an old keyboard.
        if (
            not session_id
            or session_manager.get_last_switcher_msg(user.id) == message_id
        ):
            return True
        if (
            cleanup_queue.answer_views.get((user.id, message_id), {}).get("hash")
            != version
        ):
            return True
        pages = read_pages(cleanup_queue, user.id, message_id)
        if pages is None or not 0 <= page < len(pages):
            return True
        await safe_edit(
            query,
            pages[page],
            reply_markup=keyboard(user.id, message_id, session_id, page),
        )
        view = cleanup_queue.answer_views.get((user.id, message_id))
        if view is not None:
            # No write on each tap. Absolute callback targets remain valid after a crash.
            view["page"] = page
        logger.info(
            "saved_answer_page user=%s message=%s session=%s page=%s total=%s",
            user.id,
            message_id,
            session_id,
            page + 1,
            len(pages),
        )
    return True
