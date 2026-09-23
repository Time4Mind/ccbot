"""Lifecycle helpers for abandoning the modal new-session flow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..handlers.card_registry import _carrier_edit_lock
from ..handlers.directory_browser import (
    PENDING_TEXT_KEY,
    SESSIONS_PAGE_KEY,
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
