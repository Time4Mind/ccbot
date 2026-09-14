"""Menu screen actions (CB_MM_*) - Sessions / Archive / New / Settings / Back.

Sessions is not a separate rendering — it lands on the active session's
live card. The card already has the switcher row in its footer, so it
doubles as a session-list surface.

The quota table lives directly in Menu and refreshes in the background on
each explicit Menu entry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.archive import DEFAULT_LOOKBACK_SECONDS, build_archive_page
from ...handlers.callback_data import (
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
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
from .._usage_window import (
    fetch_live_usage,
    get_cached_live_usage,
    get_cached_live_usage_age_seconds,
)

logger = logging.getLogger(__name__)

_menu_refresh_tasks: dict[int, asyncio.Task[None]] = {}
_menu_views: dict[int, Any] = {}


def render_menu_text(user_id: int, *, refreshing: bool = False) -> str:
    """Render the menu header plus the latest cached quota table."""
    from ...usage import format_usage_breakdown_compact

    status = format_usage_breakdown_compact(
        user_id,
        get_cached_live_usage(),
        age_seconds=get_cached_live_usage_age_seconds(),
        refreshing=refreshing,
    )
    return f"{render_more_text(user_id)}\n\n{status}"


def begin_menu_refresh(target: Any, user_id: int) -> None:
    """Track the visible menu and ensure one background quota refresh."""
    _menu_views[user_id] = target
    task = _menu_refresh_tasks.get(user_id)
    if task is None or task.done():
        task = asyncio.create_task(_refresh_menu_usage(user_id))
        _menu_refresh_tasks[user_id] = task


async def _refresh_menu_usage(user_id: int) -> None:
    """Refresh quota once while menu navigation remains non-blocking."""
    current = asyncio.current_task()
    try:
        await fetch_live_usage()
        target = _menu_views.get(user_id)
        if _menu_refresh_tasks.get(user_id) is current and target is not None:
            await safe_edit(
                target,
                render_menu_text(user_id),
                reply_markup=build_footer_keyboard(user_id, screen="more"),
            )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.debug("menu usage refresh failed: %s", exc)
    finally:
        if _menu_refresh_tasks.get(user_id) is current:
            _menu_refresh_tasks.pop(user_id, None)


async def _emit_new_flow(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> None:
    """Open the directory browser from the Menu screen."""
    from ...startup_queue import begin_startup_queue

    begin_startup_queue(user.id)
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
    msg_text, keyboard, subdirs = await build_directory_browser(
        start_path, user_id=user.id
    )
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


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    # Any button in the menu leaves its top-level surface. The already-started
    # quota probe may still refresh the cache, but must not repaint the next
    # screen.
    _menu_views.pop(user.id, None)

    if data == CB_MM_BACK:
        text = render_menu_text(user.id)
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
        # Compatibility for an old Telegram keyboard: Status now returns to
        # the top-level menu and refreshes its embedded quota table.
        await query.answer()
        await safe_edit(
            query,
            render_menu_text(user.id, refreshing=True),
            reply_markup=build_footer_keyboard(user.id, screen="more"),
        )
        begin_menu_refresh(query, user.id)
        return True

    if data == CB_MM_NEW:
        await query.answer()

        await _emit_new_flow(query, context, user)
        return True

    if data == CB_MM_ARCHIVE:
        await query.answer()

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
