"""Menu screen actions (CB_MM_*) — List / Status / History / Shot / New /
Archive / Settings / Back."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.archive import DEFAULT_LOOKBACK_SECONDS, build_archive_page
from ...handlers.callback_data import (
    CB_FT_CLEAR,
    CB_FT_KILL,
    CB_FT_TERM,
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_HISTORY,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_SHOT,
    CB_MM_STATUS,
)
from ...handlers.history import send_history
from ...handlers.menu import (
    build_footer_keyboard,
    can_offer_terminal,
    render_more_text,
    render_settings_text,
)
from ...handlers.message_sender import safe_send
from ...handlers.switcher import build_switcher_keyboard
from ...i18n import t
from ...session import session_manager
from .._common import active_window, set_view
from .._usage_window import fetch_claude_usage
from ..commands.info import emit_screenshot_compact
from ..commands.lifecycle import build_live_sessions_text

logger = logging.getLogger(__name__)


def build_list_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render the ``/list`` body + keyboard for ``user_id``.

    Factored out so the "Open terminal" callback (in footer.py) can
    re-render the same screen after a spawn — Telegram's
    ``editMessageText`` needs both the new text and the new markup, so
    the duplication has to happen somewhere.
    """
    body = build_live_sessions_text(user_id) or t(user_id, "list.empty")
    rows: list[list[InlineKeyboardButton]] = []
    active_sess = session_manager.get_active_session(user_id)
    if active_sess is not None and active_sess.window_id:
        ctl_row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(t(user_id, "btn.kill"), callback_data=CB_FT_KILL),
            InlineKeyboardButton(t(user_id, "btn.clear"), callback_data=CB_FT_CLEAR),
        ]
        if can_offer_terminal(user_id):
            ctl_row.append(
                InlineKeyboardButton(t(user_id, "btn.term"), callback_data=CB_FT_TERM)
            )
        rows.append(ctl_row)
    sw = build_switcher_keyboard(user_id, include_lost=True)
    if sw is not None:
        for sw_row in sw.inline_keyboard:
            rows.append(list(sw_row))
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_BACK)]
    )
    return body, InlineKeyboardMarkup(rows)


async def _emit_new_flow(
    query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> None:
    """Open the directory browser from the Menu screen."""
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
    from pathlib import Path

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
        context.user_data["menu_origin"] = "menu"
    try:
        await query.edit_message_text(text=msg_text, reply_markup=keyboard)
    except Exception:
        await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_MM_BACK:
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await set_view(query, context.bot, user.id, text, keyboard)
        await query.answer()
        return True

    if data == CB_MM_LIST:
        await query.answer()
        # /list view is a management surface (kill / clear / open-terminal
        # / switch). Real-time interrupt belongs on the live card where
        # the busy signal is fresh — here we always show Kill, otherwise
        # the button would freeze on whichever state was true at the
        # moment the menu was opened.
        body, kb = build_list_view(user.id)
        try:
            await query.edit_message_text(text=body, reply_markup=kb)
        except Exception as e:
            logger.debug("mm list edit failed: %s", e)
        return True

    if data == CB_MM_STATUS:
        await query.answer()
        base = build_footer_keyboard(user.id, screen="more", exclude_more="status")
        base_rows = list(base.inline_keyboard) if base is not None else []
        refresh_row = [
            InlineKeyboardButton(t(user.id, "btn.refresh"), callback_data=CB_MM_STATUS)
        ]
        kb = InlineKeyboardMarkup([refresh_row] + [list(r) for r in base_rows])
        try:
            await query.edit_message_text(
                text=t(user.id, "usage.fetching"), reply_markup=kb
            )
        except Exception as e:
            logger.debug("mm status placeholder failed: %s", e)
        usage_info = await fetch_claude_usage()
        from ...usage import format_usage_breakdown_compact

        live_block = format_usage_breakdown_compact(user.id, usage_info)
        text = live_block or t(user.id, "usage.unavailable")
        try:
            await query.edit_message_text(text=text, reply_markup=kb)
        except Exception as e:
            logger.debug("mm status edit failed: %s", e)
        return True

    if data == CB_MM_HISTORY:
        await query.answer()
        wid = active_window(user.id)
        if not wid:
            kb = build_footer_keyboard(user.id, screen="more", exclude_more="history")
            try:
                await query.edit_message_text(
                    text=t(user.id, "toast.no_session"), reply_markup=kb
                )
            except Exception as e:
                logger.debug("mm history empty edit failed: %s", e)
        else:
            extra_kb = build_footer_keyboard(
                user.id, screen="more", exclude_more="history"
            )
            extra_rows = list(extra_kb.inline_keyboard) if extra_kb is not None else []
            await send_history(
                target=query,
                window_id=wid,
                edit=True,
                user_id=user.id,
                extra_rows=[list(r) for r in extra_rows],
            )
        return True

    if data == CB_MM_SHOT:
        await query.answer()
        await emit_screenshot_compact(query, context.bot, user.id)
        return True

    if data == CB_MM_NEW:
        await query.answer()
        await _emit_new_flow(query, context, user)
        return True

    if data == CB_MM_ARCHIVE:
        await query.answer()
        if context.user_data is not None:
            context.user_data["_arc_show_all"] = False
        text, kb = build_archive_page(
            page=0, lookback_seconds=DEFAULT_LOOKBACK_SECONDS, show_all=False
        )
        rows: list[list[InlineKeyboardButton]] = [list(r) for r in kb.inline_keyboard]
        rows.append(
            [InlineKeyboardButton(t(user.id, "btn.back"), callback_data=CB_MM_BACK)]
        )
        await set_view(query, context.bot, user.id, text, InlineKeyboardMarkup(rows))
        return True

    if data == CB_MM_SETTINGS:
        text = render_settings_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="settings")
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("settings open edit failed: %s", e)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    return False
