"""Node menu callbacks."""

from __future__ import annotations

from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_NODE_USE
from ...session import session_manager
from .._common import open_sessions_in_place


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    if not data.startswith(CB_NODE_USE):
        return False
    node_id = data[len(CB_NODE_USE) :]
    node = session_manager.get_node(node_id)
    if node is None:
        await query.answer("Node not found", show_alert=True)
        return True
    if node.state in ("offline", "pending"):
        await query.answer("Node is unavailable", show_alert=True)
        return True
    session_manager.set_selected_node(user.id, node_id)
    await query.answer()
    await open_sessions_in_place(query, context.bot, user.id)
    return True


__all__ = ["handle"]
