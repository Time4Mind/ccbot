"""Inline session switcher callbacks (CB_SW_USE / CB_SW_NEW / CB_SW_NOOP)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_SW_NEW, CB_SW_NOOP, CB_SW_USE
from ...handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    build_directory_browser,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_send
from ...handlers.notifications import detach_paused_cards_at_message
from ...session import session_manager
from .._common import render_session_preview

logger = logging.getLogger(__name__)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_SW_NOOP:
        await query.answer("already active")
        return True

    if data.startswith(CB_SW_USE):
        target_id = data[len(CB_SW_USE) :]
        sess = session_manager.get_session(target_id)
        if sess is None or sess.state not in ("active", "idle"):
            await query.answer("Session not available", show_alert=True)
            return True
        session_manager.set_active_session(user.id, target_id)
        # Carrier message is about to host the new active session's
        # preview — release any older session's pause bound to it so
        # the displaced session resumes normal card rendering on its
        # next event instead of buffering silently.
        if query.message is not None:
            detach_paused_cards_at_message(user.id, query.message.message_id)

        # Switcher preview is a management surface, not real-time control.
        # Always render Kill — Stop lives on the live card where _card_is_busy
        # tracks whether a task is actually running. Polling
        # ``is_window_busy`` here gave false positives on completed
        # sessions (parse_status_line flickers between tool calls).
        try:
            preview = await render_session_preview(sess)
            await query.edit_message_text(text=preview)
        except Exception as e:
            logger.debug("preview edit_message_text failed: %s", e)
        keyboard = build_footer_keyboard(user.id, screen="main", is_busy=False)
        if keyboard is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception as e:
                logger.debug("preview reply markup failed: %s", e)
            else:
                if query.message:
                    session_manager.set_last_switcher_msg(
                        user.id, query.message.message_id
                    )
        await query.answer(f"→ {sess.name or sess.id}")
        return True

    if data == CB_SW_NEW:
        clear_browse_state(context.user_data)
        clear_window_picker_state(context.user_data)
        clear_session_picker_state(context.user_data)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["menu_origin"] = "main"
        try:
            await query.edit_message_text(text=msg_text, reply_markup=keyboard)
        except Exception:
            await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    return False
