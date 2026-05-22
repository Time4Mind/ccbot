"""Interactive-UI navigation (CB_ASK_*).

Up / Down / Left / Right / Esc / Enter / Space / Tab / Refresh — drive
Claude's native interactive prompts (AskUserQuestion / ExitPlanMode /
permission menus) from a Telegram inline keyboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from ...handlers.interactive_ui import clear_interactive_msg, handle_interactive_ui
from ...handlers.notifications import enter_kb_mode, exit_kb_mode, has_pending_kb
from ...session import session_manager
from ...terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
)
from ...tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


_NAV = (
    (CB_ASK_UP, "Up", "↑"),
    (CB_ASK_DOWN, "Down", "↓"),
    (CB_ASK_LEFT, "Left", "←"),
    (CB_ASK_RIGHT, "Right", "→"),
    (CB_ASK_ENTER, "Enter", "⏎ Enter"),
    (CB_ASK_SPACE, "Space", "␣ Space"),
    (CB_ASK_TAB, "Tab", "⇥ Tab"),
)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    for prefix, tmux_key, toast in _NAV:
        if data.startswith(prefix):
            window_id = data[len(prefix) :]
            w = await tmux_manager.find_window_by_id(window_id)
            if w:
                await tmux_manager.send_keys(
                    w.window_id, tmux_key, enter=False, literal=False
                )
                await asyncio.sleep(0.5)
                await _refresh_after_key(context.bot, user.id, window_id)
            await query.answer(
                toast if prefix in (CB_ASK_ENTER, CB_ASK_SPACE, CB_ASK_TAB) else ""
            )
            return True

    if data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
            # Floating-msg flow tracks an explicit msg-per-window;
            # card-based kb-mode is cleared by exit_kb_mode. Cover both.
            await clear_interactive_msg(user.id, context.bot, window_id)
            sess = session_manager.find_session_by_window(window_id)
            if sess is not None:
                has_prompt, in_kb = has_pending_kb(user.id, sess.id)
                if has_prompt or in_kb:
                    await exit_kb_mode(context.bot, user.id, sess, clear_pending=True)
        await query.answer("⎋ Esc")
        return True

    if data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        await _refresh_after_key(context.bot, user.id, window_id)
        await query.answer("🔄")
        return True

    return False


async def _refresh_after_key(bot: Any, user_id: int, window_id: str) -> None:
    """Re-paint the interactive surface after a key press.

    Active session in kb-mode → re-enter kb-mode on the card with the
    new pane snapshot (1s status_polling lag would otherwise leave the
    keyboard stale). Otherwise fall back to the floating-msg flow.
    """
    sess = session_manager.find_session_by_window(window_id)
    if sess is None:
        await handle_interactive_ui(bot, user_id, window_id)
        return
    active = session_manager.get_active_session(user_id)
    if active is not None and active.id == sess.id:
        # Card-based kb-mode for the active session: capture pane, if
        # the UI is still up re-enter; if it cleared, drop kb-mode.
        w = await tmux_manager.find_window_by_id(window_id)
        if w is None:
            return
        pane = await tmux_manager.capture_pane(w.window_id)
        if pane and is_interactive_ui(pane):
            content_obj = extract_interactive_content(pane)
            if content_obj is not None:
                await enter_kb_mode(
                    bot, user_id, sess, content_obj.content, content_obj.name
                )
                return
        has_prompt, in_kb = has_pending_kb(user_id, sess.id)
        if has_prompt or in_kb:
            await exit_kb_mode(bot, user_id, sess, clear_pending=True)
        return
    await handle_interactive_ui(bot, user_id, window_id)
