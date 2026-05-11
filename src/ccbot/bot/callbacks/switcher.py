"""Inline session switcher callbacks (CB_SW_USE / CB_SW_NEW / CB_SW_NOOP)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram.ext import ContextTypes

from ...handlers import bg_status
from ...handlers.callback_data import CB_SW_NEW, CB_SW_NOOP, CB_SW_USE
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
from ...handlers.history import send_history
from ...handlers.interactive_ui import (
    adopt_interactive_msg,
    render_interactive_keyboard,
)
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_send
from ...handlers.notifications import refresh_panel, transfer_card_to_carrier
from ...session import session_manager
from ...terminal_parser import extract_interactive_content, is_interactive_ui
from ...tmux_manager import tmux_manager
from .._common import render_session_preview
from .more_menu import (
    HISTORY_ORIGIN_KEY,
    IN_LIST_VIEW_KEY,
    build_list_view,
)

logger = logging.getLogger(__name__)


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_SW_NOOP:
        await query.answer("already active")
        return True

    if data.startswith(CB_SW_USE):
        target_id = data[len(CB_SW_USE) :]
        sess = session_manager.get_session(target_id)
        if sess is None or sess.state not in ("active", "idle"):
            await query.answer("Session not available", show_alert=True)
            return True
        logger.info(
            "sw_use user=%d target=%s name=%s state=%s carrier_msg=%s",
            user.id,
            target_id,
            sess.name,
            sess.state,
            query.message.message_id if query.message else None,
            extra={
                "event": "sw_use",
                "user_id": user.id,
                "target_session_id": target_id,
                "target_name": sess.name,
                "target_state": sess.state,
                "carrier_msg_id": query.message.message_id if query.message else None,
            },
        )

        # Hand the carrier message off from the previously-active session
        # to the newly-active one BEFORE flipping ``active_sessions``.
        # Otherwise the old session's pending events would still edit the
        # carrier (clobbering the preview we're about to paint), and the
        # detach-only fix from #29 created stray chat messages instead.
        # Pausing FROM + claiming TO keeps everything on the same carrier.
        old_active = session_manager.get_active_session(user.id)
        old_active_id = old_active.id if old_active is not None else None
        if query.message is not None:
            transfer_card_to_carrier(
                user.id,
                old_active_id,
                target_id,
                query.message.message_id,
            )

        session_manager.set_active_session(user.id, target_id)

        # If the user is currently looking at the /list management view
        # (Menu → List), keep them there: re-render the list with the
        # new session marked active. Painting history here would shift
        # the keyboard layout (Back row drops, Stop/Clear/Menu row
        # appears at the top) — the management surface is the more
        # useful affordance in this context.
        in_list_view = (
            context.user_data is not None
            and context.user_data.get(IN_LIST_VIEW_KEY) is True
        )
        if in_list_view and query.message is not None:
            body, list_kb = build_list_view(user.id)
            try:
                await query.edit_message_text(text=body, reply_markup=list_kb)
                session_manager.set_last_switcher_msg(user.id, query.message.message_id)
            except Exception as e:
                logger.debug("list-view re-render after switch failed: %s", e)
            bg_status.mark_seen(user.id, target_id)
            bg_status.prune_seen(user.id)
            await refresh_panel(context.bot, user.id)
            await query.answer(f"→ {sess.name or sess.id}")
            return True

        # If this bg session has a stashed AskUserQuestion / ExitPlanMode /
        # permission prompt, paint that UI on the carrier instead of the
        # standard preview. Re-verify against the live pane first — claude
        # may have moved on while the badge was up.
        showed_interactive_ui = False
        pending_ui = bg_status.get_pending_interactive_ui(user.id, target_id)
        if pending_ui is not None and sess.window_id and query.message is not None:
            w = await tmux_manager.find_window_by_id(sess.window_id)
            if w:
                pane = await tmux_manager.capture_pane(w.window_id)
                if pane and is_interactive_ui(pane):
                    content_obj = extract_interactive_content(pane)
                    if content_obj is not None:
                        kb = render_interactive_keyboard(
                            sess.window_id, content_obj.name
                        )
                        try:
                            await query.edit_message_text(
                                text=content_obj.content, reply_markup=kb
                            )
                            adopt_interactive_msg(
                                user.id,
                                sess.window_id,
                                query.message.message_id,
                            )
                            showed_interactive_ui = True
                        except Exception as e:
                            logger.debug("pending UI edit_message_text failed: %s", e)

        if not showed_interactive_ui:
            # Paint the session's full transcript history onto the carrier
            # so the user lands on context immediately — no extra ⋯ Menu →
            # History tap. The footer + switcher rows come along as
            # ``extra_rows`` below the pagination row, so management
            # controls stay reachable.
            #
            # On the next claude event, ``update_session_card`` will
            # repaint the carrier with the live card; the history page is
            # ephemeral by design.
            if context.user_data is not None:
                # Remember how we got into the history view so
                # CB_HISTORY_PREV/NEXT can rebuild the matching extras
                # row stack — otherwise pagination loses every button
                # except Older/Newer.
                context.user_data[HISTORY_ORIGIN_KEY] = "switcher"
            footer_kb = build_footer_keyboard(user.id, screen="main", is_busy=False)
            extra_rows = (
                [list(r) for r in footer_kb.inline_keyboard]
                if footer_kb is not None
                else None
            )
            history_painted = False
            if sess.window_id:
                try:
                    await send_history(
                        target=query,
                        window_id=sess.window_id,
                        edit=True,
                        user_id=user.id,
                        extra_rows=extra_rows,
                    )
                    history_painted = True
                except Exception as e:
                    logger.debug("switch history paint failed: %s", e)

            if not history_painted:
                # Fallback: session has no window yet (lost/archived
                # restore in flight, etc.). Fall back to the legacy
                # short preview so the user at least sees the header.
                try:
                    preview = await render_session_preview(sess)
                    await query.edit_message_text(text=preview)
                except Exception as e:
                    logger.debug("preview edit_message_text failed: %s", e)
                if footer_kb is not None:
                    try:
                        await query.edit_message_reply_markup(reply_markup=footer_kb)
                    except Exception as e:
                        logger.debug("preview reply markup failed: %s", e)

            if query.message and footer_kb is not None:
                session_manager.set_last_switcher_msg(user.id, query.message.message_id)

        # Panel housekeeping: the user has now "seen" this session, so any
        # finalised badge can leave the panel; the previously-active
        # session keeps its working/finished status for the panel render.
        bg_status.mark_seen(user.id, target_id)
        bg_status.prune_seen(user.id)
        # Active card may need a re-render to drop the just-seen entry.
        await refresh_panel(context.bot, user.id)

        await query.answer(f"→ {sess.name or sess.id}")
        return True

    if data == CB_SW_NEW:
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
            context.user_data["menu_origin"] = "main"
        try:
            await query.edit_message_text(text=msg_text, reply_markup=keyboard)
        except Exception:
            await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    return False
