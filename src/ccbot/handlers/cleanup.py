"""Unified cleanup API for session state.

Provides centralized cleanup that coordinates state across all modules,
preventing memory leaks when a session is archived, killed, or its tmux
window vanishes externally.

Functions:
  - clear_session_state: Clean up all in-memory state for a (user, window) pair.
"""

from telegram import Bot

from .interactive_ui import clear_interactive_msg, clear_interactive_for_window
from .message_queue import clear_status_msg_info, clear_tool_msg_ids_for_window


async def clear_session_state(
    user_id: int,
    window_id: str,
    bot: Bot | None = None,
) -> None:
    """Clear all in-memory state associated with a (user, window) pair.

    Called when:
      - A session is archived (auto-idle TTL or `/done`/`/kill`).
      - A tmux window vanishes externally.

    Cleans up:
      - status message tracking
      - tool_use → message_id tracking
      - interactive UI message and active-marker (best effort delete from chat)
    """
    clear_status_msg_info(user_id, window_id)
    clear_tool_msg_ids_for_window(user_id, window_id)

    if bot is not None:
        await clear_interactive_msg(user_id, bot, window_id)
    else:
        clear_interactive_for_window(user_id, window_id)
