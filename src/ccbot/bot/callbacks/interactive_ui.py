"""Interactive-UI navigation (CB_ASK_*).

Up / Down / Left / Right / Esc / Enter / Space / Tab / Refresh — drive
Claude's native interactive prompts (AskUserQuestion / ExitPlanMode /
permission menus) from a Telegram inline keyboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
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
                await handle_interactive_ui(context.bot, user.id, window_id)
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
            await clear_interactive_msg(user.id, context.bot, window_id)
        await query.answer("⎋ Esc")
        return True

    if data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer("🔄")
        return True

    return False
