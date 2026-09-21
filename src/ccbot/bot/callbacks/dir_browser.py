"""Directory browser + session picker callbacks (CB_DIR_*, CB_SESSION_*)."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_CREATE,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_NEW_BACKEND,
    CB_SESSION_BACK,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_PAGE,
    CB_SESSION_SELECT,
)
from ...handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_NODE_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    SESSIONS_PAGE_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
    build_directory_browser,
    build_session_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from ...handlers.message_sender import safe_edit
from ...handlers.notifications import resume_card_view
from ...i18n import t
from ...session import session_manager
from ...transfer_runtime import get_node_runtime
from .._common import open_more_in_place
from ..messages import create_and_activate_session


async def open_new_session_flow(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    origin: str,
) -> None:
    """Choose a backend when needed, then open the directory browser."""
    node_id = session_manager.get_selected_node_id(user_id)
    enabled = session_manager.get_effective_backends(user_id, node_id)
    if context.user_data is not None:
        context.user_data["menu_origin"] = origin
        context.user_data["_new_session_node_id"] = node_id
    only_backend = next(iter(enabled), None)
    if only_backend is None:
        await safe_edit(query, f"❌ Node {node_id} has no available backend")
        return
    if len(enabled) > 1:
        keyboard = build_backend_picker(user_id, node_id=node_id)
        await safe_edit(query, t(user_id, "backend.choose"), reply_markup=keyboard)
        return
    if context.user_data is not None:
        context.user_data["_new_session_backend"] = only_backend
    await open_directory_browser(query, context, user_id, node_id=node_id)


def build_backend_picker(
    user_id: int, *, node_id: str | None = None
) -> InlineKeyboardMarkup:
    node_id = node_id or session_manager.get_selected_node_id(user_id)
    enabled = session_manager.get_effective_backends(user_id, node_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    name.capitalize(), callback_data=f"{CB_NEW_BACKEND}{name}"
                )
                for name in enabled
            ],
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_DIR_CANCEL)],
        ]
    )


async def open_directory_browser(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    node_id: str | None = None,
) -> None:
    msg_text, keyboard, _subdirs = await initialize_directory_browser(
        context, user_id, node_id=node_id
    )
    await safe_edit(query, msg_text, reply_markup=keyboard)


async def _load_directory_browser(
    *, node_id: str, current_path: str, page: int, user_id: int
) -> tuple[str, InlineKeyboardMarkup, list[str], str]:
    if node_id == "local":
        path = current_path or str(Path.home())
        text, keyboard, subdirs = await build_directory_browser(
            path, page, user_id=user_id
        )
        return text, keyboard, subdirs, path
    runtime = get_node_runtime(node_id)
    if runtime is None:
        raise RuntimeError(f"Node runtime is not connected: {node_id}")
    result = await runtime.list_directories(node_id, current_path)
    path = str(result.get("path") or current_path or "/")
    subdirs = [str(name) for name in result.get("directories", [])]
    text, keyboard, _ = await build_directory_browser(
        path,
        page,
        user_id=user_id,
        remote_subdirs=subdirs,
        remote=True,
    )
    return text, keyboard, subdirs, path


async def initialize_directory_browser(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    node_id: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    node_id = node_id or session_manager.get_selected_node_id(user_id)
    clear_browse_state(context.user_data)
    clear_session_picker_state(context.user_data)
    clear_window_picker_state(context.user_data)
    msg_text, keyboard, subdirs, start_path = await _load_directory_browser(
        node_id=node_id, current_path="", page=0, user_id=user_id
    )
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_NODE_KEY] = node_id
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    return msg_text, keyboard, subdirs


async def resolve_session_summaries(
    sessions: list[Any], *, user_id: int
) -> dict[str, str]:
    """Agent session id → description taken from the user's messages."""
    del user_id  # Kept in the API because callers already have it available.
    return {s.session_id: s.summary or "untitled" for s in sessions}


async def emit_session_picker(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    sessions: list[Any],
    *,
    page: int,
    user_id: int,
) -> None:
    """Render (or re-render) the session picker for the cached `sessions`."""
    summaries = await resolve_session_summaries(sessions, user_id=user_id)

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
    """Leave the new-session flow and restore the active live card."""
    if context.user_data is not None:
        context.user_data.pop("menu_origin", None)
    active = session_manager.get_active_session(user_id)
    if active is not None:
        await resume_card_view(context.bot, user_id, active)
    else:
        await open_more_in_place(query, user_id)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data.startswith(CB_NEW_BACKEND):
        backend = data[len(CB_NEW_BACKEND) :]
        flow_node_id = (context.user_data or {}).get("_new_session_node_id")
        if (
            not flow_node_id
            or flow_node_id != session_manager.get_selected_node_id(user.id)
            or backend
            not in session_manager.get_effective_backends(user.id, flow_node_id)
        ):
            await query.answer("Backend is no longer available", show_alert=True)
            return True
        if context.user_data is not None:
            context.user_data["_new_session_backend"] = backend
        await open_directory_browser(query, context, user.id, node_id=str(flow_node_id))
        await query.answer()
        return True

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
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        if node_id == "local":
            new_path = (Path(current_path) / subdir_name).resolve()
            if not new_path.exists() or not new_path.is_dir():
                await query.answer("Directory not found", show_alert=True)
                return True
            new_path_str = str(new_path)
        else:
            new_path_str = posixpath.join(current_path, subdir_name)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs, resolved_path = await _load_directory_browser(
            node_id=node_id, current_path=new_path_str, page=0, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = resolved_path
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
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        parent_path = (
            str(Path(current_path).resolve().parent)
            if node_id == "local"
            else posixpath.dirname(current_path)
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs, resolved_path = await _load_directory_browser(
            node_id=node_id, current_path=parent_path, page=0, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = resolved_path
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
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs, resolved_path = await _load_directory_browser(
            node_id=node_id, current_path=current_path, page=pg, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = resolved_path
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
            msg_text, keyboard, subdirs = await build_directory_browser(
                start_path, user_id=user.id
            )
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
                context.user_data[BROWSE_PATH_KEY] = start_path
                context.user_data[BROWSE_NODE_KEY] = "local"
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            await safe_edit(query, msg_text, reply_markup=keyboard)
            return True

        if (
            context.user_data is not None
            and context.user_data.get("_directory_selection_target")
            == "default_session"
        ):
            context.user_data.pop("_directory_selection_target", None)
            clear_browse_state(context.user_data)
            session_manager.update_user_setting(
                user.id, "default_session_directory", selected_path
            )
            from ...default_session import ensure_default_session
            from ...handlers.menu import (
                build_footer_keyboard,
                render_settings_group_text,
            )
            import asyncio

            await query.answer(t(user.id, "toast.saved"))
            await safe_edit(
                query,
                render_settings_group_text(user.id, "settings_default_directory"),
                reply_markup=build_footer_keyboard(
                    user.id, screen="settings_default_directory"
                ),
            )
            asyncio.create_task(ensure_default_session(context.bot, user.id))
            return True

        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        clear_browse_state(context.user_data)
        await query.answer()
        if node_id == "local":
            await create_and_activate_session(query, context, user, selected_path)
        else:
            await create_and_activate_session(
                query, context, user, selected_path, node_id=node_id
            )
        return True

    if data == CB_DIR_CREATE:
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY) if context.user_data else None
        )
        if not selected_path:
            await query.answer("Directory selection expired", show_alert=True)
            return True
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_NAMING_DIRECTORY
        page = (
            int(context.user_data.get(BROWSE_PAGE_KEY, 0))
            if context.user_data is not None
            else 0
        )
        await safe_edit(
            query,
            t(user.id, "dir.create.prompt", path=selected_path),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            t(user.id, "btn.back"),
                            callback_data=f"{CB_DIR_PAGE}{page}",
                        )
                    ]
                ]
            ),
        )
        await query.answer()
        return True

    if data == CB_DIR_CANCEL:
        if (
            context.user_data is not None
            and context.user_data.get("_directory_selection_target")
            == "default_session"
        ):
            context.user_data.pop("_directory_selection_target", None)
            clear_browse_state(context.user_data)
            from ...handlers.menu import (
                build_footer_keyboard,
                render_settings_group_text,
            )

            await safe_edit(
                query,
                render_settings_group_text(user.id, "settings_default_directory"),
                reply_markup=build_footer_keyboard(
                    user.id, screen="settings_default_directory"
                ),
            )
            await query.answer()
            return True
        from ...startup_queue import cancel_startup_queue

        unsent = cancel_startup_queue(user.id)
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_new_session_backend", None)
        await _close_modal(query, user.id, context)
        await query.answer(
            f"Cancelled; {unsent} queued item(s) were not sent" if unsent else None,
            show_alert=bool(unsent),
        )
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
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        kwargs = {"node_id": node_id} if node_id != "local" else {}
        await query.answer()
        await create_and_activate_session(
            query,
            context,
            user,
            selected_path,
            resume_session_id=session.session_id,
            **kwargs,
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
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await query.answer()
        if node_id == "local":
            await create_and_activate_session(query, context, user, selected_path)
        else:
            await create_and_activate_session(
                query, context, user, selected_path, node_id=node_id
            )
        return True

    if data == CB_SESSION_CANCEL:
        from ...startup_queue import cancel_startup_queue

        unsent = cancel_startup_queue(user.id)
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop(SESSIONS_PAGE_KEY, None)
        clear_browse_state(context.user_data)
        await _close_modal(query, user.id, context)
        await query.answer(
            f"Cancelled; {unsent} queued item(s) were not sent" if unsent else None,
            show_alert=bool(unsent),
        )
        return True

    if data == CB_SESSION_BACK:
        selected_path = (
            context.user_data.get("_selected_path", str(Path.home()))
            if context.user_data
            else str(Path.home())
        )
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop(SESSIONS_PAGE_KEY, None)

        msg_text, keyboard, subdirs, resolved_path = await _load_directory_browser(
            node_id=node_id, current_path=selected_path, page=0, user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = resolved_path
            context.user_data[BROWSE_NODE_KEY] = node_id
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
