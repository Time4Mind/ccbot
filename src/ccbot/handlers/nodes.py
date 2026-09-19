"""Node menu rendering and selection helpers."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t
from ..node_models import Node
from ..session import session_manager
from .callback_data import CB_MM_BACK, CB_NODE_USE


def _node_state_text(user_id: int, node: Node) -> str:
    return t(user_id, f"nodes.state.{node.state}")


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
        backends = ", ".join(node.backends) or "-"
        lines.append(
            f"| {marker}{node.display_name or node.id} | "
            f"{_node_state_text(user_id, node)} | {backends} |"
        )
    return "\n".join(lines)


def build_nodes_keyboard(user_id: int) -> InlineKeyboardMarkup:
    selected_id = session_manager.get_selected_node_id(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    for node in session_manager.list_nodes():
        label = f"{'✓ ' if node.id == selected_id else ''}{node.display_name or node.id}"
        if node.state in ("offline", "pending"):
            rows.append([InlineKeyboardButton(f"⚪ {label}")])
        else:
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"{CB_NODE_USE}{node.id}")]
            )
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_BACK)]
    )
    return InlineKeyboardMarkup(rows)


__all__ = ["build_nodes_keyboard", "render_nodes_text"]
