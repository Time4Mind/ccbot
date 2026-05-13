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
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_HISTORY,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_SHOT,
    CB_MM_STATUS,
    CB_SW_NEW,
)

# CB_MM_SHOT is no longer surfaced from the Menu grid (per UX request: the
# screenshot button now lives in the /list view next to Kill / Clear, since
# /list shows the active session's transcript and that's the right surface
# for "snapshot the terminal" too). The CB_MM_SHOT callback handler itself
# is unchanged — taps from the new button hit the same code path.
from ...handlers.history import send_history
from ...handlers.menu import (
    build_footer_keyboard,
    render_more_text,
    render_settings_text,
)
from ...handlers.message_sender import safe_edit, safe_send
from ...handlers.switcher import build_switcher_keyboard
from ...i18n import t
from ...session import session_manager
from .._common import active_window, set_view
from .._usage_window import fetch_claude_usage
from ..commands.info import emit_screenshot_compact
from ..commands.lifecycle import build_live_sessions_text

logger = logging.getLogger(__name__)


# user_data marker key: ``_history_origin`` lets the history-pagination
# callback rebuild the same extra-row footer the history was painted
# with originally, so the buttons under the pagination row don't vanish
# on a page click. Values: ``"switcher"`` (carrier was opened by a
# session switcher tap) / ``"more"`` (Menu → History).
HISTORY_ORIGIN_KEY = "_history_origin"


def clear_view_markers(user_data: dict[str, Any] | None) -> None:
    """Drop the history-origin marker — call on any navigation that
    leaves the history view (CB_MM_BACK, text typed, Menu re-opened,
    a Menu sub-screen that isn't History)."""
    if user_data is None:
        return
    user_data.pop(HISTORY_ORIGIN_KEY, None)


def build_list_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render the ``/list`` body + keyboard for ``user_id``.

    Factored out so callers that re-render the same screen (e.g. after
    a session-level action) don't duplicate the keyboard layout.
    """
    body = build_live_sessions_text(user_id) or t(user_id, "list.empty")
    rows: list[list[InlineKeyboardButton]] = []
    active_sess = session_manager.get_active_session(user_id)
    if active_sess is not None and active_sess.window_id:
        ctl_row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(t(user_id, "btn.kill"), callback_data=CB_FT_KILL),
            InlineKeyboardButton(t(user_id, "btn.clear"), callback_data=CB_FT_CLEAR),
            InlineKeyboardButton(t(user_id, "mm.shot"), callback_data=CB_MM_SHOT),
        ]
        rows.append(ctl_row)
    sw = build_switcher_keyboard(user_id, include_lost=True, include_new=False)
    if sw is not None:
        for sw_row in sw.inline_keyboard:
            rows.append(list(sw_row))
    # `+ new` shares the bottom row with Back (the navigation slot in the
    # /list view) so the two go-elsewhere affordances sit side-by-side,
    # matching the main-screen `[+ new] [≡ Menu]` pair.
    rows.append(
        [
            InlineKeyboardButton("+ new", callback_data=CB_SW_NEW),
            InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_BACK),
        ]
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
        await safe_edit(query, msg_text, reply_markup=keyboard)
    except Exception:
        await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_MM_BACK:
        clear_view_markers(context.user_data)
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await set_view(query, context.bot, user.id, text, keyboard)
        await query.answer()
        return True

    if data == CB_MM_LIST:
        await query.answer()
        # Menu → List paints the ACTIVE session's transcript on the
        # carrier with the /list footer (Kill / Clear / switcher rows /
        # ＋ new / Back). The session names + token usage already live
        # in the switcher row labels — duplicating them in the body
        # made this view feel useless ("just the same list again").
        # Showing history makes List the natural "where am I" surface.
        #
        # Fallback to the legacy body-list view when there's no active
        # session (e.g. user has only archived ones).
        clear_view_markers(context.user_data)
        active_sess = session_manager.get_active_session(user.id)
        body, kb = build_list_view(user.id)
        if active_sess is None or not active_sess.window_id:
            await safe_edit(query, body, reply_markup=kb)
            return True
        if context.user_data is not None:
            # Mark origin so the history-pagination handler rebuilds
            # THIS view's footer (Kill / Clear / switcher / + new /
            # Back), not the main-screen one with ≡ Menu.
            context.user_data[HISTORY_ORIGIN_KEY] = "menu_list"
        extra_rows = [list(r) for r in kb.inline_keyboard]
        try:
            await send_history(
                target=query,
                window_id=active_sess.window_id,
                edit=True,
                user_id=user.id,
                extra_rows=extra_rows,
            )
        except Exception as e:
            logger.debug("mm list history paint failed: %s", e)
            await safe_edit(query, body, reply_markup=kb)
        return True

    if data == CB_MM_STATUS:
        await query.answer()
        clear_view_markers(context.user_data)
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

    if data == CB_MM_HISTORY:
        await query.answer()
        clear_view_markers(context.user_data)
        if context.user_data is not None:
            context.user_data[HISTORY_ORIGIN_KEY] = "more"
        wid = active_window(user.id)
        if not wid:
            kb = build_footer_keyboard(user.id, screen="more", exclude_more="history")
            await safe_edit(query, t(user.id, "toast.no_session"), reply_markup=kb)
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
        clear_view_markers(context.user_data)
        await emit_screenshot_compact(query, context.bot, user.id)
        return True

    if data == CB_MM_NEW:
        await query.answer()
        clear_view_markers(context.user_data)
        await _emit_new_flow(query, context, user)
        return True

    if data == CB_MM_ARCHIVE:
        await query.answer()
        clear_view_markers(context.user_data)
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
        clear_view_markers(context.user_data)
        text = render_settings_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="settings")
        await safe_edit(query, text, reply_markup=keyboard)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    return False
