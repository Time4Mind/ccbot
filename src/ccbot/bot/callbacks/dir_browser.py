"""Directory browser + session picker callbacks (CB_DIR_*, CB_SESSION_*).

Also owns the readable-session-summary cache machinery used when the
user has Settings → Previews set to ``readable``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_BACK,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_PAGE,
    CB_SESSION_SELECT,
)
from ...handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    SESSIONS_PAGE_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    build_directory_browser,
    build_session_picker,
    clear_browse_state,
    clear_session_picker_state,
)
from ...handlers.message_sender import safe_edit
from ...naming import generate_name
from ...session import session_manager
from .._common import open_more_in_place
from ..messages import create_and_activate_session

logger = logging.getLogger(__name__)


async def _resolve_session_summaries(
    sessions: list[Any], *, user_id: int
) -> dict[str, str]:
    """claude_session_id → display summary, honoring user Previews setting."""
    settings = session_manager.get_user_settings(user_id)
    mode = settings.get("previews", "economical")
    out: dict[str, str] = {}
    if mode != "readable":
        for s in sessions:
            out[s.session_id] = s.summary or "untitled"
        return out

    pending: list[tuple[str, str, float]] = []  # (sid, seed, mtime)
    for s in sessions:
        try:
            mtime = Path(s.file_path).stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = session_manager.get_cached_summary(s.session_id, mtime)
        if cached:
            out[s.session_id] = cached
        else:
            out[s.session_id] = s.summary or "untitled"
            seed = (s.summary or "")[:200]
            if seed:
                pending.append((s.session_id, seed, mtime))

    if pending:

        async def _bg() -> None:
            for sid, seed, mtime in pending:
                try:
                    name = await generate_name(seed)
                    if name:
                        readable = name.replace("-", " ")
                        session_manager.set_cached_summary(sid, readable, mtime)
                except Exception as e:
                    logger.debug("haiku resolve failed for %s: %s", sid, e)

        asyncio.create_task(_bg())
    return out


async def emit_session_picker(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    sessions: list[Any],
    *,
    page: int,
    user_id: int,
) -> None:
    """Render (or re-render) the session picker for the cached `sessions`."""
    summaries = await _resolve_session_summaries(sessions, user_id=user_id)

    def _resolver(s: Any) -> str:
        return summaries.get(s.session_id, s.summary or "untitled")

    text, keyboard = build_session_picker(
        sessions, page=page, summary_resolver=_resolver, user_id=user_id
    )
    if context.user_data is not None:
        context.user_data[SESSIONS_PAGE_KEY] = page
    await safe_edit(query, text, reply_markup=keyboard)


async def _close_modal(
    query: CallbackQuery, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Bail out of any modal flow → always Menu."""
    if context.user_data is not None:
        context.user_data.pop("menu_origin", None)
    await open_more_in_place(query, user_id)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data.startswith(CB_DIR_SELECT):
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return True

        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return True
        subdir_name = cached_dirs[idx]

        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()
        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return True

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    if data == CB_DIR_UP:
        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        parent_path = str(Path(current_path).resolve().parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    if data.startswith(CB_DIR_PAGE):
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return True
        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(
            current_path, pg, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    if data == CB_DIR_CONFIRM:
        # Stale-click guard: if the user's directory-browser state was
        # wiped (bot restart between open and confirm, or they tapped
        # Select on a long-scrolled-up old message), ``BROWSE_PATH_KEY``
        # is missing and the old code silently fell back to
        # ``Path.home()`` — creating a session in the wrong directory.
        # Surface the click as expired and re-open a fresh browser at
        # home instead of silently picking ``/root``.
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY) if context.user_data else None
        )
        if not selected_path:
            await query.answer(
                "Directory selection expired — pick again", show_alert=True
            )
            start_path = str(Path.home())
            msg_text, keyboard, subdirs = build_directory_browser(
                start_path, user_id=user.id
            )
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
                context.user_data[BROWSE_PATH_KEY] = start_path
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            await safe_edit(query, msg_text, reply_markup=keyboard)
            return True

        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if sessions:
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                context.user_data[SESSIONS_KEY] = sessions
                context.user_data["_selected_path"] = selected_path
                context.user_data[SESSIONS_PAGE_KEY] = 0
            await emit_session_picker(query, context, sessions, page=0, user_id=user.id)
            await query.answer()
            return True

        clear_browse_state(context.user_data)
        await create_and_activate_session(query, context, user, selected_path)
        return True

    if data == CB_DIR_CANCEL:
        clear_browse_state(context.user_data)
        await _close_modal(query, user.id, context)
        await query.answer()
        return True

    if data.startswith(CB_SESSION_SELECT):
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return True

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return True

        session = cached_sessions[idx]
        # Stale-click guard — see CB_DIR_CONFIRM for why.
        selected_path = (
            context.user_data.get("_selected_path") if context.user_data else None
        )
        if not selected_path:
            await query.answer(
                "Session selection expired — pick again", show_alert=True
            )
            clear_session_picker_state(context.user_data)
            return True
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await create_and_activate_session(
            query, context, user, selected_path, resume_session_id=session.session_id
        )
        return True

    if data == CB_SESSION_NEW:
        # Stale-click guard — see CB_DIR_CONFIRM for why.
        selected_path = (
            context.user_data.get("_selected_path") if context.user_data else None
        )
        if not selected_path:
            await query.answer(
                "Session selection expired — pick again", show_alert=True
            )
            clear_session_picker_state(context.user_data)
            return True
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await create_and_activate_session(query, context, user, selected_path)
        return True

    if data == CB_SESSION_CANCEL:
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop(SESSIONS_PAGE_KEY, None)
        clear_browse_state(context.user_data)
        await _close_modal(query, user.id, context)
        await query.answer()
        return True

    if data == CB_SESSION_BACK:
        selected_path = (
            context.user_data.get("_selected_path", str(Path.home()))
            if context.user_data
            else str(Path.home())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop(SESSIONS_PAGE_KEY, None)

        msg_text, keyboard, subdirs = build_directory_browser(
            selected_path, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = selected_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    if data.startswith(CB_SESSION_PAGE):
        try:
            pg = int(data[len(CB_SESSION_PAGE) :])
        except ValueError:
            await query.answer("Invalid page")
            return True
        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if not cached_sessions:
            await query.answer("Session list expired, please retry", show_alert=True)
            return True
        await emit_session_picker(
            query, context, cached_sessions, page=pg, user_id=user.id
        )
        await query.answer()
        return True

    return False
