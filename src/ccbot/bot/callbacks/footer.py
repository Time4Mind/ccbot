"""Footer callbacks (CB_FT_STOP / KILL / CLEAR / MORE / TERM) — top row of the live card."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
    CB_FT_CLEAR,
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_STOP,
    CB_FT_TERM,
)
from ...handlers.menu import build_footer_keyboard, render_more_text
from ...handlers.notifications import clear_card, pause_card_view
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
        # whatever screen they're looking at. The pause is auto-released
        # by ``resume_card_view`` from text_handler when the user types
        # the next message, so no explicit "close" button is needed.
        sess = session_manager.get_active_session(user.id)
        if sess is not None:
            pause_card_view(user.id, sess.id)
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await set_view(query, context.bot, user.id, text, keyboard)
        await query.answer()
        return True

    if data == CB_FT_TERM:
        # Manual "Open terminal" — spawns a native Terminal/iTerm tab on
        # macOS or the user's configured emulator on Linux, attached to
        # the active session's tmux window. The button lives on the
        # footer top row alongside Stop/Kill/Clear/Menu, gated on
        # ``can_offer_terminal``. A stale tap (race between the user
        # tapping and a terminal already arriving) is harmless — the
        # spawn just adds another attached client at the desired window.
        from ...local_terminal import open_terminal_for_window

        sess = session_manager.get_active_session(user.id)
        if sess is None or not sess.window_id:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        await open_terminal_for_window(sess.window_id, user_id=user.id)
        await query.answer(t(user.id, "toast.term_opened"))
        # Refresh the footer keyboard on the current message so the Term
        # button drops out (next render will see the attached client).
        keyboard = build_footer_keyboard(user.id, screen="main", is_busy=False)
        if keyboard is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception as e:
                logger.debug("term post-spawn keyboard refresh failed: %s", e)
        return True

    return False
