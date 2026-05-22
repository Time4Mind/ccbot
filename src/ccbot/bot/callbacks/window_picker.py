"""Window picker callbacks (CB_WIN_BIND / CB_WIN_NEW / CB_WIN_CANCEL).

The window picker is the legacy "bind an unbound tmux window to a Session"
flow — kept around for /new with workdir conflicts and tmux-server restarts
where windows persist but Session records vanished.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_WIN_BIND, CB_WIN_CANCEL, CB_WIN_NEW
from ...handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    clear_window_picker_state,
)
from ...handlers.message_sender import safe_edit, safe_send
from ...session import session_manager
from ...tmux_manager import tmux_manager
from .._common import open_more_in_place

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data.startswith(CB_WIN_BIND):
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return True

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return True
        selected_wid = cached_windows[idx]

        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return True

        display = w.window_name
        clear_window_picker_state(context.user_data)

        # Adopt the unbound window into a fresh Session record and activate it.
        sess = session_manager.find_session_by_window(selected_wid)
        if sess is None:
            ws = session_manager.get_window_state(selected_wid)
            sess = session_manager.create_session(
                name=display, window_id=selected_wid, workdir=ws.cwd or ""
            )
            if ws.session_id:
                session_manager.set_session_claude_id(sess.id, ws.session_id)
        else:
            session_manager.set_session_window(sess.id, selected_wid)
        session_manager.set_active_session(user.id, sess.id)

        await safe_edit(query, f"✅ Bound to window `{display}`")

        pending_text = (
            context.user_data.get("_pending_text") if context.user_data else None
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        if pending_text:
            send_ok, send_msg = await session_manager.send_to_window(
                selected_wid, pending_text
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                await safe_send(
                    context.bot,
                    user.id,
                    f"❌ Failed to send pending message: {send_msg}",
                )
        await query.answer("Bound")
        return True

    if data == CB_WIN_NEW:
        clear_window_picker_state(context.user_data)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    if data == CB_WIN_CANCEL:
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        await open_more_in_place(query, user.id)
        await query.answer()
        return True

    return False
