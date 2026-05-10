"""Outbound routing — dispatch claude → TG events to live session cards.

A single ``handle_new_message`` is registered with ``SessionMonitor``;
each emitted ``NewMessage`` resolves the owning Session, updates that
session's live card, and on terminal-text turns calls
``finalize_task`` for the completion summary.

Also handles:
  - empty-content filter (Claude sometimes emits placeholder text/thinking
    chunks after /model or /clear; they'd ghost-edit the card without this).
  - ``INTERACTIVE_TOOL_NAMES`` short-circuit — those tools render their own
    prompt UI in TG instead of going through the live card.
  - G6 quota crossings — separate push, not card-merged.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

from ..config import config
from ..handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    handle_interactive_ui,
    set_interactive_mode,
)
from ..handlers.message_queue import get_message_queue
from ..handlers.notifications import (
    finalize_task,
    push_event,
    update_session_card,
)
from ..session import session_manager
from ..session_monitor import NewMessage
from ..usage import aggregate_session, should_warn_quota

logger = logging.getLogger(__name__)


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Route one assistant turn (or streaming chunk) into the right live card."""
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)
    if not targets:
        # Try to bind via the session_map (claude_session_id -> window_id) when a
        # Session exists for the matching window without a claude_session_id yet.
        await session_manager.load_session_map()
        targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)
    if not targets:
        logger.info("No session record for claude session %s", msg.session_id)
        return

    # Drop empty assistant placeholder turns (Claude sometimes emits them
    # right after /model or /clear) — they'd ghost-edit the card.
    if (
        msg.role == "assistant"
        and msg.content_type in ("text", "thinking")
        and not (msg.text or "").strip()
    ):
        logger.debug(
            "Dropping empty assistant %s for session=%s",
            msg.content_type,
            msg.session_id,
        )
        return

    for user_id, sess in targets:
        wid = sess.window_id
        if not wid:
            continue
        session_manager.touch_session(sess.id)

        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        # Tools that render their own UI go through the interactive-UI surface.
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            set_interactive_mode(user_id, wid)
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid)
            if handled:
                await push_event(
                    bot, user_id, sess, text=f"interactive prompt: {msg.tool_name}"
                )
                claude_sess = await session_manager.resolve_session_for_window(wid)
                if claude_sess and claude_sess.file_path:
                    try:
                        file_size = Path(claude_sess.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue
            clear_interactive_mode(user_id)

        # Any non-interactive event invalidates a previously-shown interactive UI.
        if get_interactive_msg_id(user_id, wid):
            await clear_interactive_msg(user_id, bot, wid)

        if msg.is_complete:
            # Real end-of-turn assistant text → "task complete".  Mid-stream
            # text blocks (stop_reason=tool_use) are intermediate narration —
            # those belong on the live card, not the completion summary.
            is_terminal_text = (
                msg.role == "assistant"
                and msg.content_type == "text"
                and msg.stop_reason in ("end_turn", "stop_sequence", "max_tokens")
            )
            if is_terminal_text:
                await finalize_task(bot, user_id, sess, msg.text or "")
            else:
                await update_session_card(bot, user_id, sess, msg)

            claude_sess = await session_manager.resolve_session_for_window(wid)
            if claude_sess and claude_sess.file_path:
                try:
                    file_size = Path(claude_sess.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass

            # G6 soft quota crossing — separate push, not card-merged.
            if msg.role == "assistant" and msg.content_type == "text":
                try:
                    su = await aggregate_session(sess)
                    sess.token_usage_total = su.tokens_total
                    if should_warn_quota(su):
                        pct = (
                            su.tokens_5h * 100 // max(1, config.session_token_budget_5h)
                        )
                        await push_event(
                            bot,
                            user_id,
                            sess,
                            text=(
                                f"⚠ burned {pct}% of 5h quota "
                                f"({config.session_token_budget_5h // 1000}k)"
                            ),
                        )
                except Exception as e:
                    logger.debug("quota check failed: %s", e)
        else:
            # Streaming chunk — best-effort card update.
            await update_session_card(bot, user_id, sess, msg)
