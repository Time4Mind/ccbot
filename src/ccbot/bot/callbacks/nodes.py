"""Node menu callbacks."""

from __future__ import annotations

from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_NODE_DELETE,
    CB_NODE_DELETE_CANCEL,
    CB_NODE_DELETE_CONFIRM,
    CB_NODE_DISABLE,
    CB_NODE_ENABLE,
    CB_NODE_USE,
)
from ...handlers.message_sender import safe_edit
from ...handlers.nodes import build_nodes_keyboard, render_nodes_text
from ...i18n import t
from ...session import session_manager
from ...transfer_runtime import unregister_node_runtime
from ...transfer_runtime import get_node_runtime
from .._common import open_sessions_in_place


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    for prefix, enabled in ((CB_NODE_DISABLE, False), (CB_NODE_ENABLE, True)):
        if data.startswith(prefix):
            node_id = data[len(prefix) :]
            try:
                session_manager.set_node_enabled(node_id, enabled)
            except (KeyError, ValueError):
                await query.answer(
                    t(user.id, "nodes.delete.not_found"), show_alert=True
                )
                return True
            if enabled:
                from ...node_runtime import register_remote_runtime

                try:
                    register_remote_runtime(node_id)
                except RuntimeError:
                    pass
            else:
                unregister_node_runtime(node_id)
            await safe_edit(
                query,
                render_nodes_text(user.id),
                reply_markup=build_nodes_keyboard(user.id),
            )
            await query.answer(
                t(user.id, "nodes.enable.done" if enabled else "nodes.disable.done")
            )
            return True
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
        runtime = get_node_runtime(node_id)
        if runtime is None:
            await query.answer(
                t(user.id, "nodes.delete.revoke_failed"), show_alert=True
            )
            return True
        await query.answer()
        try:
            result = await runtime.revoke_node(node_id)
        except Exception:
            await safe_edit(
                query,
                t(user.id, "nodes.delete.revoke_failed"),
                reply_markup=build_nodes_keyboard(user.id),
            )
            return True
        if result.get("ok") is not True:
            await safe_edit(
                query,
                t(user.id, "nodes.delete.revoke_failed"),
                reply_markup=build_nodes_keyboard(user.id),
            )
            return True
        try:
            session_manager.remove_node(node_id)
        except (KeyError, ValueError):
            await safe_edit(
                query,
                t(user.id, "nodes.delete.not_found"),
                reply_markup=build_nodes_keyboard(user.id),
            )
            return True
        unregister_node_runtime(node_id)
        await safe_edit(
            query,
            render_nodes_text(user.id),
            reply_markup=build_nodes_keyboard(user.id),
        )
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
    if not node.is_available():
        await query.answer("Node is unavailable", show_alert=True)
        return True
    session_manager.set_selected_node(user.id, node_id)
    await query.answer()
    await open_sessions_in_place(query, context.bot, user.id)
    return True


__all__ = ["handle"]
