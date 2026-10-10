"""Directory loading and lifecycle helpers for the modal new-session flow."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardMarkup

from ..handlers.card_registry import _carrier_edit_lock
from ..handlers.callback_data import (
    CB_DIR_UP,
    CB_DIR_CONFIRM,
    CB_DIR_CREATE,
    CB_DIR_SELECT,
    CB_DIR_PAGE,
)
from ..handlers.directory_browser import (
    BROWSE_NODE_KEY,
    PENDING_TEXT_KEY,
    SESSIONS_PAGE_KEY,
    build_directory_browser,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from ..handlers.message_sender import safe_edit
from ..startup_queue import (
    current_startup_flow,
    fail_startup_queue,
    report_failed_startup_entries,
)

logger = logging.getLogger(__name__)


async def reject_unavailable_directory_callback(
    query: Any, context: Any, manager: Any
) -> bool:
    """Recheck access before a stale browser button can issue remote work."""
    data = query.data or ""
    node_id = (context.user_data or {}).get(BROWSE_NODE_KEY, "local")
    if node_id != "local" and (
        data in (CB_DIR_UP, CB_DIR_CONFIRM, CB_DIR_CREATE)
        or data.startswith((CB_DIR_SELECT, CB_DIR_PAGE))
    ):
        node = manager.get_node(str(node_id))
        if node is None or not node.can_operate("directory_browser"):
            await query.answer(
                "Directory browsing is unavailable on this node", show_alert=True
            )
            return True
    return False


async def load_directory_browser(
    *,
    manager: Any,
    runtime_lookup: Callable[[str], Any],
    node_id: str,
    current_path: str,
    page: int,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup, list[str], str]:
    """Browse a connected node's filesystem independently of backend auth."""
    if node_id == "local":
        path = current_path or str(Path.home())
        text, keyboard, subdirs = await build_directory_browser(
            path, page, user_id=user_id
        )
        return text, keyboard, subdirs, path
    node = manager.get_node(node_id)
    if node is None or not node.can_operate("directory_browser"):
        raise RuntimeError("Directory browsing is unavailable on this node")
    runtime = runtime_lookup(node_id)
    if runtime is None:
        raise RuntimeError(f"Node runtime is not connected: {node_id}")
    result = await runtime.list_directories(node_id, current_path)
    path = str(result.get("path") or current_path or "/")
    subdirs = [str(name) for name in result.get("directories", [])]
    text, keyboard, _ = await build_directory_browser(
        path, page, user_id=user_id, remote_subdirs=subdirs, remote=True
    )
    return text, keyboard, subdirs, path


def clear_new_session_state(user_data: dict[str, Any] | None) -> None:
    """Invalidate callbacks and state belonging to the abandoned modal."""
    clear_browse_state(user_data)
    clear_session_picker_state(user_data)
    clear_window_picker_state(user_data)
    if user_data is not None:
        for key in (
            PENDING_TEXT_KEY,
            SESSIONS_PAGE_KEY,
            "_new_session_backend",
            "_new_session_node_id",
            "_pending_session_name",
            "_selected_path",
            "menu_origin",
        ):
            user_data.pop(key, None)


async def edit_startup_surface(
    query: Any,
    context: Any,
    user_id: int,
    startup_flow: Any | None,
    text: str,
    keyboard: Any,
) -> bool:
    """Paint a Start surface only while its generation still owns the UI."""
    async with _carrier_edit_lock(user_id):
        if startup_flow is not None:
            from ..startup_queue import is_current_startup_flow

            if not is_current_startup_flow(user_id, startup_flow):
                clear_new_session_state(context.user_data)
                return False
        await safe_edit(query, text, reply_markup=keyboard)
        return True


async def edit_backend_picker(
    query: Any,
    context: Any,
    user_id: int,
    startup_flow: Any | None,
    node_id: str,
) -> bool:
    """Render backend choice with the same generation guard."""
    from ..i18n import t
    from .backend_picker import build_backend_picker

    return await edit_startup_surface(
        query,
        context,
        user_id,
        startup_flow,
        t(user_id, "backend.choose"),
        build_backend_picker(user_id, node_id=node_id),
    )


def cancel_for_active_card(user_id: int, user_data: dict[str, Any] | None) -> bool:
    """Leave Start when an active-session card wins the UI race.

    An uncommitted picker queue is released synchronously before the card is
    painted. A startup already bound to its provisional session keeps running,
    but its capture is session-scoped, so input to the newly selected active
    session is not intercepted. Already captured prompts retain the established
    failed-startup notification path.
    """
    flow = current_startup_flow(user_id)
    if flow is None:
        return False

    # Once a provisional session owns the queue, inbound capture is already
    # scoped to that session. Switching elsewhere therefore unblocks the new
    # active session without cancelling an in-flight tmux/RPC creation task
    # midway (which could orphan a just-created process).
    if flow.session_id is not None or flow.window_id is not None:
        clear_new_session_state(user_data)
        logger.info(
            "new-session modal left for active card user=%d session=%s window=%s",
            user_id,
            flow.session_id,
            flow.window_id,
        )
        return True

    entries = fail_startup_queue(user_id)
    clear_new_session_state(user_data)

    if entries:
        asyncio.create_task(
            report_failed_startup_entries(entries),
            name=f"startup-cancel-report:{user_id}",
        )
    logger.info(
        "new-session flow cancelled by active card user=%d pending=%d",
        user_id,
        len(entries),
    )
    return True


__all__ = [
    "cancel_for_active_card",
    "clear_new_session_state",
    "edit_backend_picker",
    "edit_startup_surface",
]
