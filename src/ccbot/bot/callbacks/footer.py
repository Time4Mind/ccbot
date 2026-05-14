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
    CB_FT_OLDER,
    CB_FT_STOP,
    CB_FT_TERM,
)
from ...handlers.history import (
    get_cached_total_pages,
    prewarm_pages_cache,
    send_history,
)
from ...handlers.menu import build_footer_keyboard, render_more_text
from ...handlers.notifications import clear_card, pause_card_view, release_card_message
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
        # Entering Menu top-level resets any sub-screen markers
        # (`_in_list_view`, `_history_origin`).
        from .more_menu import clear_view_markers as _clear_view_markers

        _clear_view_markers(context.user_data)
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await set_view(query, context.bot, user.id, text, keyboard)
        await query.answer()
        return True

    if data == CB_FT_OLDER:
        sess = session_manager.get_active_session(user.id)
        if sess is None or not sess.window_id:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        # Make sure the pages cache is fresh so we can target page-1
        # below; this is a no-op when the cache already matches the
        # JSONL (mtime+size).
        try:
            await prewarm_pages_cache(sess.window_id)
        except Exception as e:
            logger.debug("ft older prewarm failed: %s", e)
        total = get_cached_total_pages(sess.window_id)
        if total is None:
            await query.answer("No history yet", show_alert=False)
            return True
        # Target the page BEFORE the current latest — that's what "Older"
        # means relative to the live-card view. When there's only one
        # page, fall back to it so the user still lands on the
        # paginated history surface (the pagination row will simply not
        # show ◀/▶).
        offset = max(0, total - 2)

        from .more_menu import HISTORY_ORIGIN_KEY

        if context.user_data is not None:
            context.user_data[HISTORY_ORIGIN_KEY] = "switcher"
        footer_kb = build_footer_keyboard(
            user.id, screen="main", is_busy=False, include_older_btn=False
        )
        extra_rows = (
            [list(r) for r in footer_kb.inline_keyboard]
            if footer_kb is not None
            else None
        )
        try:
            await send_history(
                target=query,
                window_id=sess.window_id,
                offset=offset,
                edit=True,
                user_id=user.id,
                extra_rows=extra_rows,
            )
        except Exception as e:
            logger.debug("ft older paint failed: %s", e)
            await query.answer("History paint failed", show_alert=True)
            return True
        # Detach the carrier from the live-card so subsequent claude
        # events don't clobber the freshly-painted history view.
        release_card_message(user.id, sess.id)
        if query.message:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
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
