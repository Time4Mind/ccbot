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
from ...handlers.message_sender import safe_edit, safe_send
from ...handlers.notifications import (
    enter_kb_mode,
    get_card_state,
    paint_card_on_carrier,
    pause_card_view,
    transfer_card_to_carrier,
)
from ...session import session_manager
from ...terminal_parser import extract_interactive_content, is_interactive_ui
from ...tmux_manager import tmux_manager
from .._common import render_session_preview

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

        # The session we just LEFT is now bg. Seed its panel row from
        # JSONL — but only for the "working" case. If the inferred
        # status is "finished", the user was just looking at that
        # session's live card and saw the answer themselves — surfacing
        # a ✅ badge for it now would be a false notification (user
        # explicitly reported: "I looked at the session result — after
        # this it should disappear"). Clear the entry instead.
        if (
            old_active is not None
            and old_active.id != target_id
            and old_active.window_id
        ):
            import asyncio as _asyncio

            from ...session_models import Session as _Session

            async def _seed_bg_status(old_sess: _Session) -> None:
                try:
                    inferred = await bg_status.infer_status_from_jsonl(old_sess)
                except Exception as e:
                    logger.debug("infer bg status failed: %s", e)
                    return
                changed = False
                if inferred == "finished":
                    # User just left an already-finished session — no
                    # notification needed; clear any stale entry.
                    changed = bg_status.clear_for_user_session(user.id, old_sess.id)
                elif inferred == "working":
                    changed = bg_status.update_status(user.id, old_sess.id, "working")
                if changed:
                    try:
                        from ...handlers.notifications import refresh_panel

                        await refresh_panel(context.bot, user.id)
                    except Exception as e:
                        logger.debug("refresh_panel after seed failed: %s", e)

            _asyncio.create_task(_seed_bg_status(old_active))

        # The switcher tap always lands the user on the session's
        # history view, regardless of which view fired it (main card,
        # /screenshot, etc.). The Menu button anchored to the bottom row
        # keeps the layout visually stable across the transition.

        # If this bg session has a stashed AskUserQuestion / ExitPlanMode /
        # permission prompt, paint kb-mode on the carrier directly.
        # Re-verify against the live pane first — claude may have moved
        # on while the badge was up.
        showed_interactive_ui = False
        pending_ui = bg_status.get_pending_interactive_ui(user.id, target_id)
        if pending_ui is not None and sess.window_id and query.message is not None:
            w = await tmux_manager.find_window_by_id(sess.window_id)
            if w:
                pane = await tmux_manager.capture_pane(w.window_id)
                if pane and is_interactive_ui(pane):
                    content_obj = extract_interactive_content(pane)
                    if content_obj is not None:
                        # Claim the carrier as the live card msg, then
                        # flip it into kb-mode view. paint_card_on_carrier
                        # sets msg_id; enter_kb_mode then edits in place.
                        try:
                            state = get_card_state(user.id, sess)
                            state.msg_id = query.message.message_id
                            state.in_menu_view = False
                            await enter_kb_mode(
                                context.bot,
                                user.id,
                                sess,
                                content_obj.content,
                                content_obj.name,
                            )
                            showed_interactive_ui = True
                        except Exception as e:
                            logger.debug("pending UI kb_mode failed: %s", e)

        if not showed_interactive_ui:
            # Switcher tap unifies with Menu → Sessions: the carrier
            # becomes the target session's LIVE CARD. No frozen JSONL
            # transcript, no release_card_message, no second message
            # spawning below on the next event.
            #
            # ``paint_card_on_carrier`` claims the carrier, seeds JSONL
            # history if state.events is empty, and renders the full
            # live-card surface (header + paginated body + bg-panel +
            # main footer). Subsequent claude events edit the same msg.
            #
            # Fallback: session has no window (lost/archived restore in
            # flight) → fall through to a short preview so the user at
            # least sees the header.
            painted = False
            if sess.window_id and query.message is not None:
                try:
                    await paint_card_on_carrier(
                        context.bot, user.id, sess, query.message.message_id
                    )
                    painted = True
                except Exception as e:
                    logger.debug("paint_card_on_carrier failed: %s", e)
            if not painted:
                try:
                    preview = await render_session_preview(sess)
                    await safe_edit(query, preview)
                except Exception as e:
                    logger.debug("preview safe_edit failed: %s", e)

        # Panel housekeeping: the user switched INTO this session, so it
        # is no longer "background" relative to them — drop its bg entry.
        bg_status.clear_for_user_session(user.id, target_id)
        # NB: do NOT call refresh_panel here. The carrier message just
        # got painted with the history view (or the pending interactive
        # UI). refresh_panel re-renders the live card on the same
        # message_id with the fresh-session state, whose ``lines`` is
        # empty — overwriting the history we just put there with a
        # header-only card. The next real claude event for the active
        # session will re-render the card naturally; the panel update
        # arrives with it.

        await query.answer(f"→ {sess.name or sess.id}")
        return True

    if data == CB_SW_NEW:
        # The entire + new flow lives on the SAME carrier message: old
        # live card → dir browser → new session's empty live card. No
        # extra "Created" notice — the message just transitions in place.
        #
        # Pause the active session first so its events buffer silently
        # while the user picks a directory; events catch up when the
        # user switches back via the switcher.
        active = session_manager.get_active_session(user.id)
        if active is not None:
            pause_card_view(user.id, active.id)
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
            await safe_edit(query, msg_text, reply_markup=keyboard)
        except Exception:
            await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)
        await query.answer()
        return True

    return False
