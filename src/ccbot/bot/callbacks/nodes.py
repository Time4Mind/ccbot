"""Node menu callbacks."""

from __future__ import annotations

from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_NODE_DELETE,
    CB_NODE_DELETE_CANCEL,
    CB_NODE_DELETE_CONFIRM,
    CB_NODE_USE,
)
from ...handlers.message_sender import safe_edit
from ...handlers.nodes import build_nodes_keyboard, render_nodes_text
from ...i18n import t
from ...session import session_manager
from ...transfer_runtime import unregister_node_runtime
from .._common import open_sessions_in_place


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    if data == CB_NODE_DELETE_CANCEL:
        await safe_edit(
            query,
            render_nodes_text(user.id),
            reply_markup=build_nodes_keyboard(user.id),
        )
        await query.answer()
        return True
    if data.startswith(CB_NODE_DELETE_CONFIRM):
        node_id = data[len(CB_NODE_DELETE_CONFIRM) :]
        node = session_manager.get_node(node_id)
        if node is None or node_id == "local":
            await query.answer(t(user.id, "nodes.delete.not_found"), show_alert=True)
            return True
        try:
            session_manager.remove_node(node_id)
        except (KeyError, ValueError):
            await query.answer(t(user.id, "nodes.delete.not_found"), show_alert=True)
            return True
        unregister_node_runtime(node_id)
        await safe_edit(
            query,
            render_nodes_text(user.id),
            reply_markup=build_nodes_keyboard(user.id),
        )
        await query.answer(t(user.id, "nodes.delete.done"))
        return True
    if data.startswith(CB_NODE_DELETE):
        node_id = data[len(CB_NODE_DELETE) :]
        node = session_manager.get_node(node_id)
        if node is None or node_id == "local":
            await query.answer(t(user.id, "nodes.delete.not_found"), show_alert=True)
            return True
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user.id, "btn.yes_delete"),
                        callback_data=f"{CB_NODE_DELETE_CONFIRM}{node_id}",
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.cancel"),
                        callback_data=CB_NODE_DELETE_CANCEL,
                    ),
                ]
            ]
        )
        await safe_edit(
            query,
            t(user.id, "nodes.delete.confirm", node=node.display_name or node.id),
            reply_markup=keyboard,
        )
        await query.answer()
        return True
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
