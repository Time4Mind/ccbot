"""Node menu rendering and selection helpers."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t
from ..node_models import Node
from ..session import session_manager
from .callback_data import (
    CB_NODE_BACK,
    CB_NODE_DELETE,
    CB_NODE_DISABLE,
    CB_NODE_ENABLE,
    CB_NODE_USE,
)

NODES_ORIGIN_KEY = "_nodes_origin"


def _node_state_text(user_id: int, node: Node) -> str:
    state = node.state if node.is_available() or node.id == "local" else "offline"
    return t(user_id, f"nodes.state.{state}")


def _node_backends(user_id: int, node: Node) -> tuple[str, ...]:
    return session_manager.get_effective_backends(user_id, node.id)


def render_nodes_text(user_id: int) -> str:
    nodes = session_manager.list_nodes()
    lines = [t(user_id, "nodes.title"), ""]
    if not nodes:
        lines.append(t(user_id, "nodes.empty"))
        return "\n".join(lines)
    lines.extend(
        (
            f"| {t(user_id, 'nodes.table.node')} | "
            f"{t(user_id, 'nodes.table.state')} | "
            f"{t(user_id, 'nodes.table.backends')} |",
            "|---|---|---|",
        )
    )
    selected_id = session_manager.get_selected_node_id(user_id)
    for node in nodes:
        marker = "✓ " if node.id == selected_id else ""
        backends = ", ".join(_node_backends(user_id, node)) or "-"
        lines.append(
            f"| {marker}{node.display_name or node.id} | "
            f"{_node_state_text(user_id, node)} | {backends} |"
        )
    return "\n".join(lines)


def build_nodes_keyboard(user_id: int) -> InlineKeyboardMarkup:
    selected_id = session_manager.get_selected_node_id(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    for node in session_manager.list_nodes():
        label = (
            f"{'✓ ' if node.id == selected_id else ''}{node.display_name or node.id}"
        )
        delete_button = (
            InlineKeyboardButton(
                t(user_id, "btn.delete"),
                callback_data=f"{CB_NODE_DELETE}{node.id}",
            )
            if node.id != "local"
            else None
        )
        if not node.is_available():
            row = [
                InlineKeyboardButton(
                    f"⚪ {label}", callback_data=f"{CB_NODE_USE}{node.id}"
                )
            ]
        else:
            row = [InlineKeyboardButton(label, callback_data=f"{CB_NODE_USE}{node.id}")]
        if delete_button is not None:
            row.append(
                InlineKeyboardButton(
                    t(user_id, "nodes.enable" if not node.enabled else "nodes.disable"),
                    callback_data=(
                        f"{CB_NODE_ENABLE}{node.id}"
                        if not node.enabled
                        else f"{CB_NODE_DISABLE}{node.id}"
                    ),
                )
            )
            row.append(delete_button)
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_NODE_BACK)]
    )
    return InlineKeyboardMarkup(rows)


__all__ = [
    "NODES_ORIGIN_KEY",
    "build_nodes_keyboard",
    "render_nodes_text",
]
