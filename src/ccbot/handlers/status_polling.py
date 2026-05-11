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
import re
import time

from telegram import Bot
from telegram.constants import ChatAction

from ..config import config
from ..session import session_manager
from ..terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
)
from ..tmux_manager import tmux_manager
from . import bg_status
from .archive import idle_archive_sweep, purge_sweep
from .cleanup import clear_session_state
from .inbox import inbox_sweep
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import get_message_queue
from .notifications import refresh_panel, touch_card_status

# Match option lines like "  1. Yes" / " ❯ 2. Yes, and don't ask again".
_OPTION_LINE_RE = re.compile(r"^[\s❯>]*?(\d+)\.\s+(.+?)\s*$")


def _parse_first_yes_option(pane_text: str) -> str | None:
    """First option line whose label starts with "Yes". Returns its number."""
    for raw in pane_text.splitlines():
        m = _OPTION_LINE_RE.match(raw)
        if not m:
            continue
        if m.group(2).lower().startswith("yes"):
            return m.group(1)
    return None


async def _maybe_auto_approve(user_id: int, window_id: str, pane_text: str) -> bool:
    """Auto-Yes on the in-pane Yes/No prompt when the user opted in.

    Returns True iff a key was sent — caller should then skip surfacing the
    UI to TG.
    """
    mode = session_manager.get_user_settings(user_id).get("auto_approve", "off")
    if mode != "on":
        return False
    digit = _parse_first_yes_option(pane_text)
    if digit is None:
        return False
    # Number-key shortcut: typing the digit picks the option, Enter submits.
    try:
        await tmux_manager.send_keys(window_id, digit, enter=True)
    except Exception as e:
        logger.debug("auto_approve send_keys failed: %s", e)
        return False
    logger.info(
        "Auto-approved interactive prompt (opt=%s) for user=%d window=%s",
        digit,
        user_id,
        window_id,
    )
    return True


logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds — fast feedback; rate limiting is at send layer.

# Idle-archive sweep cadence (seconds).
ARCHIVE_SWEEP_INTERVAL = 60.0
# Archive-purge sweep cadence (seconds).
PURGE_SWEEP_INTERVAL = 3600.0


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    skip_status: bool = False,
) -> None:
    """Poll terminal: detect interactive UIs and refresh the card's status badge.

    There is no separate "status message" anymore — the live session card
    already shows the session's state. The pane status line ("…Esc to
    interrupt", "Working…") is folded into the card header via
    notifications.touch_card_status when present.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    interactive_window = get_interactive_window(user_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through.
        await clear_interactive_msg(user_id, bot, window_id)
        should_check_new_ui = False

    sess = session_manager.find_session_by_window(window_id)
    active = session_manager.get_active_session(user_id)
    # Treat orphan windows (no session record) as "active-like": there's
    # no bg-status row to flip, so falling through to the legacy handler
    # is the safer default. Only suppress when we know this is a
    # background-session window for a different active session.
    is_bg_session = sess is not None and active is not None and active.id != sess.id

    # Check for permission prompt (interactive UI not triggered via JSONL).
    if (
        should_check_new_ui
        and interactive_window is None
        and is_interactive_ui(pane_text)
    ):
        if is_bg_session and sess is not None:
            # Background session: never surface the prompt in chat. Stash
            # the snapshot in bg_status and flip ❓ on the panel. The
            # switcher-tap handler renders it when the user looks at the
            # session.
            content_obj = extract_interactive_content(pane_text)
            ui_tuple = (
                (content_obj.content, content_obj.name)
                if content_obj is not None
                else None
            )
            if bg_status.update_status(
                user_id, sess.id, "needs_action", interactive_ui=ui_tuple
            ):
                await refresh_panel(bot, user_id)
            return

        # User-configurable auto-approve takes precedence — bypass the TG
        # surface entirely when the setting matches.
        if await _maybe_auto_approve(user_id, window_id, pane_text):
            return
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s)",
            user_id,
            window_id,
        )
        await handle_interactive_ui(bot, user_id, window_id)
        return

    # No interactive UI on this pane right now. If we previously stashed
    # one for a bg session (e.g. claude dismissed the prompt without our
    # input), clear it so the ❓ badge doesn't lie.
    if is_bg_session and sess is not None and not is_interactive_ui(pane_text):
        if bg_status.clear_pending_ui(user_id, sess.id):
            await refresh_panel(bot, user_id)

    # Lift status into the card header. Skip when skip_status to avoid
    # piling on top of an active enqueued event.
    if skip_status:
        return
    status_line = parse_status_line(pane_text) or ""
    # Telegram chat-action ("typing…") for the user when the active
    # session is actually producing output. Telegram refreshes the
    # indicator every ~5s, and our poll cadence is 1s, so a quiet pane
    # stops showing "typing" automatically. Skipped for bg sessions —
    # only the foreground session's busy state should bubble to chat.
    if status_line and not is_bg_session and sess is not None:
        try:
            await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        except Exception as e:
            logger.debug("send_chat_action TYPING failed: %s", e)
    await touch_card_status(bot, user_id, window_id, status_line)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for every live session."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_archive_sweep = 0.0
    last_purge_sweep = 0.0
    while True:
        try:
            now = time.monotonic()

            # Idle-TTL sweep — archive sessions whose last_event_at exceeded TTL.
            if now - last_archive_sweep >= ARCHIVE_SWEEP_INTERVAL:
                last_archive_sweep = now
                for user_id in sorted(config.allowed_users):
                    try:
                        await idle_archive_sweep(bot, user_id)
                    except Exception as e:
                        logger.debug("idle_archive_sweep error: %s", e)

            # Long-archive purge sweep.
            if now - last_purge_sweep >= PURGE_SWEEP_INTERVAL:
                last_purge_sweep = now
                try:
                    purge_sweep()
                except Exception as e:
                    logger.debug("purge_sweep error: %s", e)
                try:
                    inbox_sweep()
                except Exception as e:
                    logger.debug("inbox_sweep error: %s", e)

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
