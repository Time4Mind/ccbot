"""Terminal status line polling for active and idle sessions.

Provides background polling of terminal status lines for all the bot's
single user's live sessions:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates per-session status messages in Telegram
  - Reaps tmux windows that vanished externally — marks Session as `lost`
    and cleans up in-memory state

Key components:
  - STATUS_POLL_INTERVAL: polling frequency (1 s)
  - status_poll_loop: background polling task
  - update_status_message: poll a single window and enqueue updates
"""

import asyncio
import logging

from telegram import Bot

from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .cleanup import clear_session_state
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import enqueue_status_update, get_message_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds — fast feedback; rate limiting is at send layer.


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(bot, user_id, window_id, None)
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure — keep existing status message
        return

    interactive_window = get_interactive_window(user_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, window_id)
        should_check_new_ui = False

    # Check for permission prompt (interactive UI not triggered via JSONL).
    # Only check for new UI if no interactive UI is shown for any other window
    # (we don't want to overwrite a different window's UI).
    if (
        should_check_new_ui
        and interactive_window is None
        and is_interactive_ui(pane_text)
    ):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s)",
            user_id,
            window_id,
        )
        await handle_interactive_ui(bot, user_id, window_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(bot, user_id, window_id, status_line)
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for every live session."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            # Iterate every (user, window) pair derived from active+idle sessions.
            pairs: list[tuple[int, str]] = []
            for user_id in sorted(config.allowed_users):
                for sess in session_manager.list_user_sessions(
                    user_id, states=("active", "idle")
                ):
                    if sess.window_id:
                        pairs.append((user_id, sess.window_id))

            for user_id, wid in pairs:
                try:
                    # Reap tmux windows that vanished externally.
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        sess = session_manager.find_session_by_window(wid)
                        if sess is not None:
                            session_manager.mark_session_lost(sess.id)
                        await clear_session_state(user_id, wid, bot)
                        logger.info(
                            "Reaped lost window: user=%d window_id=%s",
                            user_id,
                            wid,
                        )
                        continue

                    # UI detection happens unconditionally inside update_status_message.
                    # Status enqueue is skipped when interactive UI is detected
                    # (returns early) or when the queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        "Status update error for user %d window %s: %s",
                        user_id,
                        wid,
                        e,
                    )
        except Exception as e:
            logger.error("Status poll loop error: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
