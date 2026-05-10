"""History pagination callbacks (CB_HISTORY_PREV / CB_HISTORY_NEXT)."""

from __future__ import annotations

import logging
from typing import Any

from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from ...handlers.history import send_history
from ...handlers.message_sender import safe_edit
from ...tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""
    if not (data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT)):
        return False

    prefix_len = len(CB_HISTORY_PREV)  # same length for prev / next
    rest = data[prefix_len:]
    try:
        parts = rest.split(":")
        if len(parts) < 4:
            offset_str, window_id = rest.split(":", 1)
            start_byte, end_byte = 0, 0
        else:
            offset_str = parts[0]
            start_byte = int(parts[-2])
            end_byte = int(parts[-1])
            window_id = ":".join(parts[1:-2])
        offset = int(offset_str)
    except (ValueError, IndexError):
        await query.answer("Invalid data")
        return True

    w = await tmux_manager.find_window_by_id(window_id)
    if w:
        await send_history(
            query,
            window_id,
            offset=offset,
            edit=True,
            start_byte=start_byte,
            end_byte=end_byte,
        )
    else:
        await safe_edit(query, "Window no longer exists.")
    await query.answer("Page updated")
    return True
