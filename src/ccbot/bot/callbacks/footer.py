"""Footer callbacks (CB_FT_STOP / KILL / CLEAR / MORE) — top row of the live card."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
    CB_FT_CLEAR,
    CB_FT_CLOSE,
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_STOP,
    CB_FT_TERM,
)
from ...handlers.menu import build_footer_keyboard, render_more_text
from .more_menu import build_list_view
from ...handlers.notifications import clear_card, pause_card_view, resume_card_view
from ...i18n import t
from ...session import session_manager
from ...tmux_manager import tmux_manager
from .._common import active_window, set_view

logger = logging.getLogger(__name__)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_FT_STOP:
        wid = active_window(user.id)
        if not wid:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            await query.answer(t(user.id, "toast.window_gone"), show_alert=False)
            return True
        await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
        await query.answer(t(user.id, "toast.esc_sent"))
        return True

    if data == CB_FT_KILL:
        sess = session_manager.get_active_session(user.id)
        if sess is None or sess.state not in ("active", "idle", "lost"):
            await query.answer(t(user.id, "toast.nothing_to_kill"), show_alert=False)
            return True
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user.id, "btn.yes_kill"),
                        callback_data=f"{CB_CONF_KILL_YES}{sess.id}"[:64],
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.no"), callback_data=CB_CONF_KILL_NO
                    ),
                ]
            ]
        )
        await set_view(
            query, context.bot, user.id, t(user.id, "conf.kill", name=sess.name), kb
        )
        await query.answer()
        return True

    if data == CB_FT_CLEAR:
        wid = active_window(user.id)
        if not wid:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            await query.answer(t(user.id, "toast.window_gone"), show_alert=False)
            return True
        success, message = await session_manager.send_to_window(wid, "/clear")
        if not success:
            await query.answer(f"Clear failed: {message}", show_alert=True)
            return True
        session_manager.clear_window_session(wid)
        # Wipe the live card body so the previous turn's tool log
        # doesn't sit there pretending to be the current state.
        sess = session_manager.get_active_session(user.id)
        if sess is not None:
            await clear_card(context.bot, user.id, sess)
        await query.answer(t(user.id, "toast.cleared"))
        return True

    if data == CB_FT_MORE:
        # Pause the active session's live card so events buffered while
        # the user navigates the Menu / sub-screens don't repaint over
        # whatever screen they're looking at. resume on CB_FT_CLOSE.
        sess = session_manager.get_active_session(user.id)
        if sess is not None:
            pause_card_view(user.id, sess.id)
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await set_view(query, context.bot, user.id, text, keyboard)
        await query.answer()
        return True

    if data == CB_FT_CLOSE:
        # Resume the active session's live card with whatever events
        # accumulated during the menu navigation.
        sess = session_manager.get_active_session(user.id)
        if sess is not None:
            await resume_card_view(context.bot, user.id, sess)
        await query.answer()
        return True

    if data == CB_FT_TERM:
        # Manual "Open terminal" — spawns a native Terminal/iTerm tab on
        # macOS or the user's configured emulator on Linux, attached to
        # the active session's tmux window. The button lives on /list
        # and is only rendered when the user's setting permits it AND
        # no client is currently attached to this window's group; a
        # stale tap (race between the user opening /list and a terminal
        # already arriving) is harmless because the spawn just adds
        # another attached client at the desired window.
        from ...local_terminal import open_terminal_for_window

        sess = session_manager.get_active_session(user.id)
        if sess is None or not sess.window_id:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        await open_terminal_for_window(sess.window_id, user_id=user.id)
        await query.answer(t(user.id, "toast.term_opened"))
        # Re-render the /list view so the button disappears now that a
        # client is (about to be) attached.
        text, keyboard = build_list_view(user.id)
        await set_view(query, context.bot, user.id, text, keyboard)
        return True

    return False
