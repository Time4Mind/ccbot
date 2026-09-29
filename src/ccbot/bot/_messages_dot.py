"""Route the single-dot shortcut to a native terminal confirmation."""

from __future__ import annotations

from typing import Any

from ..handlers.message_sender import safe_reply
from ..terminal_parser import is_interactive_ui
from ..terminal_runtime import PaneCaptureError, capture_window_pane
from .callbacks.interactive_ui import confirm_interactive_selection


async def route_dot(
    message: Any,
    bot: Any,
    user_id: int,
    window_id: str,
    *,
    manager: Any,
    tmux: Any,
    runtime_getter: Any,
) -> bool | None:
    """Confirm a visible picker; return None to send the dot as regular text."""
    try:
        pane = await capture_window_pane(
            window_id,
            manager=manager,
            tmux=tmux,
            runtime_getter=runtime_getter,
        )
    except PaneCaptureError as exc:
        await safe_reply(
            message,
            f"❌ Could not verify the session terminal: {exc}. Your message wasn't sent.",
        )
        return False
    if not is_interactive_ui(pane):
        return None
    if not await confirm_interactive_selection(bot, user_id, window_id):
        await safe_reply(message, "❌ Could not send Enter to the session.")
        return False
    return True
