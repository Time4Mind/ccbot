"""Menu screen actions (CB_MM_*) — Sessions / Status / Shot / New /
Archive / Settings / Back.

Sessions is not a separate rendering — it lands on the active session's
live card. The card already has the switcher row in its footer, so it
doubles as a session-list surface.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.archive import DEFAULT_LOOKBACK_SECONDS, build_archive_page
from ...handlers.callback_data import (
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_SHOT,
    CB_MM_STATUS,
    CB_SW_NEW,
)
from ...handlers.menu import (
    build_footer_keyboard,
    render_more_text,
    render_settings_text,
)
from ...handlers.message_sender import safe_edit, safe_send
from ...handlers.notifications import paint_card_on_carrier
from ...i18n import t
from ...session import session_manager
from .._common import set_view
from .._usage_window import fetch_claude_usage
from ..commands.info import emit_screenshot_compact

logger = logging.getLogger(__name__)


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
        await safe_edit(query, msg_text, reply_markup=keyboard)
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
        # Menu → Sessions is not a separate screen — it lands on the
        # active session's live card (which already carries the switcher
        # row + in-card pagination). Single rendering, one surface.

        active_sess = session_manager.get_active_session(user.id)
        if active_sess is None or not active_sess.window_id:
            # No active session — thin empty-state with [+ new][Back].
            empty_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("+ new", callback_data=CB_SW_NEW),
                        InlineKeyboardButton(
                            t(user.id, "btn.back"), callback_data=CB_MM_BACK
                        ),
                    ]
                ]
            )
            await safe_edit(query, t(user.id, "list.empty"), reply_markup=empty_kb)
            return True
        carrier_msg_id = query.message.message_id if query.message else None
        if carrier_msg_id is None:
            return True
        try:
            await paint_card_on_carrier(
                context.bot, user.id, active_sess, carrier_msg_id
            )
        except Exception as e:
            logger.debug("mm sessions paint failed: %s", e)
            await safe_edit(query, t(user.id, "list.empty"))
        return True

    if data == CB_MM_STATUS:
        await query.answer()

        base = build_footer_keyboard(user.id, screen="more", exclude_more="status")
        base_rows = list(base.inline_keyboard) if base is not None else []
        refresh_row = [
            InlineKeyboardButton(t(user.id, "btn.refresh"), callback_data=CB_MM_STATUS)
        ]
        kb = InlineKeyboardMarkup([refresh_row] + [list(r) for r in base_rows])
        await safe_edit(query, t(user.id, "usage.fetching"), reply_markup=kb)
        usage_info = await fetch_claude_usage()
        from ...usage import format_usage_breakdown_compact

        live_block = format_usage_breakdown_compact(user.id, usage_info)
        text = live_block or t(user.id, "usage.unavailable")
        await safe_edit(query, text, reply_markup=kb)
        return True

    if data == CB_MM_SHOT:
        await query.answer()
        # Screenshot's Back button always returns to the main live-card
        # view now — the legacy "l" origin (Menu→List) doesn't exist
        # anymore since Menu→Sessions IS the live card.

        await emit_screenshot_compact(query, context.bot, user.id, origin="m")
        return True

    if data == CB_MM_NEW:
        await query.answer()

        await _emit_new_flow(query, context, user)
        return True

    if data == CB_MM_ARCHIVE:
        await query.answer()

        if context.user_data is not None:
            context.user_data["_arc_show_all"] = False
        text, kb = await build_archive_page(
            page=0,
            lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
            show_all=False,
            user_id=user.id,
            back_callback=CB_MM_BACK,
        )
        await set_view(query, context.bot, user.id, text, kb)
        return True

    if data == CB_MM_SETTINGS:
        text = render_settings_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="settings")
        await safe_edit(query, text, reply_markup=keyboard)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    return False
