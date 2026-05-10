"""Confirmation dialogs (CB_CONF_KILL/DONE/DEL × YES/NO).

After Yes/No the carrier message edits back to Menu — keeps the chat
clean instead of leaving "Cancelled" / "Killed X" stubs lying around.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram.ext import ContextTypes

from ...handlers.callback_data import (
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


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
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

    return False
