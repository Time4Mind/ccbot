"""History pagination callbacks (CB_HISTORY_PREV / CB_HISTORY_NEXT).

Reattaches the same extras-row stack that was painted under the
history page originally — the originating callback (CB_SW_USE or
CB_MM_HISTORY) writes a marker into ``context.user_data`` so we know
whether the user opened history from the switcher tap or from
Menu → History, and can rebuild a matching footer.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from ...handlers.history import send_history
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_edit
from ...tmux_manager import tmux_manager
from .more_menu import HISTORY_ORIGIN_KEY

logger = logging.getLogger(__name__)


def _build_extra_rows(user_id: int, origin: str) -> list[list[Any]] | None:
    """Rebuild the extras-row stack a freshly-painted history view would
    have had, so a page click doesn't drop the footer."""
    if origin == "more":
        kb = build_footer_keyboard(user_id, screen="more", exclude_more="history")
    else:
        # "switcher" or unknown: fall back to the main footer + switcher.
        kb = build_footer_keyboard(user_id, screen="main", is_busy=False)
    if kb is None:
        return None
    return [list(r) for r in kb.inline_keyboard]


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

    origin = (
        context.user_data.get(HISTORY_ORIGIN_KEY, "switcher")
        if context.user_data is not None
        else "switcher"
    )
    extra_rows = _build_extra_rows(user.id, origin)

    w = await tmux_manager.find_window_by_id(window_id)
    if w:
        await send_history(
            query,
            window_id,
            offset=offset,
            edit=True,
            start_byte=start_byte,
            end_byte=end_byte,
            extra_rows=extra_rows,
        )
    else:
        await safe_edit(query, "Window no longer exists.")
    await query.answer("Page updated")
    return True
