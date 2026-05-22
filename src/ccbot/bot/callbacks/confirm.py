"""Confirmation dialogs (CB_CONF_KILL/DONE/DEL × YES/NO).

After Yes/No the carrier message edits back to Menu — keeps the chat
clean instead of leaving "Cancelled" / "Killed X" stubs lying around.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_CONF_CLEAR_NO,
    CB_CONF_CLEAR_YES,
    CB_CONF_DEL_NO,
    CB_CONF_DEL_YES,
    CB_CONF_DONE_NO,
    CB_CONF_DONE_YES,
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
)
from ...i18n import t
from ...session import session_manager
from .._common import open_more_in_place
from ..commands.lifecycle import archive_session

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data.startswith(CB_CONF_KILL_YES):
        sid = data[len(CB_CONF_KILL_YES) :]
        sess = session_manager.get_session(sid)
        if sess is None or sess.state not in ("active", "idle", "lost"):
            await open_more_in_place(query, user.id)
            await query.answer(t(user.id, "toast.already_gone"), show_alert=False)
            return True
        await archive_session(user.id, context.bot, sess, completed=False)
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "toast.killed"))
        return True

    if data == CB_CONF_KILL_NO:
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "btn.cancelled"))
        return True

    if data.startswith(CB_CONF_DONE_YES):
        sid = data[len(CB_CONF_DONE_YES) :]
        sess = session_manager.get_session(sid)
        if sess is None or sess.state not in ("active", "idle"):
            await open_more_in_place(query, user.id)
            await query.answer("Not live", show_alert=False)
            return True
        await archive_session(user.id, context.bot, sess, completed=True)
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "toast.done"))
        return True

    if data == CB_CONF_DONE_NO:
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "btn.cancelled"))
        return True

    if data.startswith(CB_CONF_DEL_YES):
        sid = data[len(CB_CONF_DEL_YES) :]
        if session_manager.delete_session(sid):
            await open_more_in_place(query, user.id)
            await query.answer(t(user.id, "toast.deleted"))
        else:
            await open_more_in_place(query, user.id)
            await query.answer(t(user.id, "toast.already_gone"), show_alert=False)
        return True

    if data == CB_CONF_DEL_NO:
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "btn.cancelled"))
        return True

    if data.startswith(CB_CONF_CLEAR_YES):
        # Yes → Stop (Esc) then /clear so claude lands on a clean
        # prompt regardless of whether something was in flight.
        sid = data[len(CB_CONF_CLEAR_YES) :]
        sess = session_manager.get_session(sid)
        if sess is None or sess.state not in ("active", "idle") or not sess.window_id:
            await open_more_in_place(query, user.id)
            await query.answer(t(user.id, "toast.already_gone"), show_alert=False)
            return True
        from ...handlers.notifications import clear_card, resume_card_view
        from ...tmux_manager import tmux_manager

        w = await tmux_manager.find_window_by_id(sess.window_id)
        if not w:
            await open_more_in_place(query, user.id)
            await query.answer(t(user.id, "toast.window_gone"), show_alert=False)
            return True
        # Esc to interrupt anything in flight — short pause so the
        # prompt redraws before /clear lands.
        await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
        import asyncio as _asyncio

        await _asyncio.sleep(0.4)
        success, message = await session_manager.send_to_window(
            sess.window_id, "/clear"
        )
        if not success:
            await open_more_in_place(query, user.id)
            await query.answer(f"Clear failed: {message}", show_alert=True)
            return True
        session_manager.clear_window_session(sess.window_id)
        await clear_card(query.get_bot(), user.id, sess)
        await resume_card_view(query.get_bot(), user.id, sess)
        await query.answer(t(user.id, "toast.cleared"))
        return True

    if data == CB_CONF_CLEAR_NO:
        await open_more_in_place(query, user.id)
        await query.answer(t(user.id, "btn.cancelled"))
        return True

    return False
