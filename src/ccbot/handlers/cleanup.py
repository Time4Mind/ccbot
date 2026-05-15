"""Unified cleanup API for session state.

Provides centralized cleanup that coordinates state across modules,
preventing memory leaks when a session is archived, killed, or its
tmux window vanishes externally.

Functions:
  - clear_session_state: Clean up all in-memory state for a (user, window) pair.
"""

from telegram import Bot

from .interactive_ui import clear_interactive_for_window, clear_interactive_msg


async def clear_session_state(
    user_id: int,
    window_id: str,
    bot: Bot | None = None,
) -> None:
    """Clear in-memory state for a (user, window) pair.

    Called when:
      - A session is archived (auto-idle TTL or `/done`/`/kill`).
      - A tmux window vanishes externally.
    """
    if bot is not None:
        await clear_interactive_msg(user_id, bot, window_id)
    else:
        clear_interactive_for_window(user_id, window_id)
