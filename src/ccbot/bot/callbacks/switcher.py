"""Inline session switcher callbacks (CB_SW_USE / CB_SW_NEW / CB_SW_NOOP)."""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram import CallbackQuery
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...handlers import bg_status
from ...handlers.card_binding import bind_carrier
from ...handlers.callback_data import CB_SW_NEW, CB_SW_NOOP, CB_SW_USE
from ...handlers.card_types import CarrierKind
from ...handlers.message_sender import safe_edit, safe_send
from ...handlers.notifications import (
    activate_card_on_carrier,
    enter_kb_mode,
    get_card_state,
    paint_card_on_carrier,
    pause_card_view,
    refresh_cached_screenshot,
)
from ...handlers.card_carrier import SCREENSHOT_CACHE_FRESH_SECONDS
from ...session import session_manager
from ...terminal_parser import extract_interactive_content, is_interactive_ui
from .interactive_ui import capture_window
from .._common import render_session_preview

logger = logging.getLogger(__name__)


async def _strip_orphan_switcher_if_current(
    bot: Any, user_id: int, orphan_msg_id: int | None
) -> bool:
    """Strip an orphan only while it is still the registered live keyboard."""
    if (
        orphan_msg_id is None
        or session_manager.get_last_switcher_msg(user_id) != orphan_msg_id
    ):
        return False
    try:
        await bot.edit_message_reply_markup(
            chat_id=user_id, message_id=orphan_msg_id, reply_markup=None
        )
        return True
    except BadRequest as exc:
        error = str(exc).lower()
        if "message to edit not found" in error or "message is not modified" in error:
            return True
        logger.debug("current orphan switcher strip failed: %s", exc)
    except Exception as exc:
        logger.debug("current orphan switcher strip failed: %s", exc)
    return False


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
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
        # A completed result stays unread through its first presentation and
        # becomes acknowledged only when the user enters it a second time.
        # Record before painting so that second entry already shows ☑️.
        bg_status.record_finished_view(user.id, target_id)
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
        # to the newly-active one behind the carrier-edit barrier.  This
        # drains an old editMessageText already in flight, then pauses FROM,
        # claims TO, and flips ``active_sessions`` atomically so a late update
        # from FROM cannot overwrite the target after it is painted.
        old_active = session_manager.get_active_session(user.id)
        old_active_id = old_active.id if old_active is not None else None
        orphan_msg_id: int | None = None
        if query.message is not None:
            orphan_msg_id = await activate_card_on_carrier(
                user.id,
                old_active_id,
                target_id,
                query.message.message_id,
            )
        else:
            session_manager.select_session(user.id, target_id)
        # Dismiss Telegram's tap spinner before any screenshot render/upload.
        # The carrier is already atomically owned by the target session here.
        await query.answer(f"→ {sess.name or sess.id}")
        # The TO session already had a live card on a DIFFERENT message
        # (typical after a voice message pinned to it finished landing
        # while the user was elsewhere). It just lost ownership to the
        # carrier, so nothing will ever edit it again — strip its
        # keyboard, otherwise the chat keeps two tappable switchers and
        # the next tap repaints whichever one the user happened to hit.
        await _strip_orphan_switcher_if_current(context.bot, user.id, orphan_msg_id)

        # The session we just LEFT remains represented in the switcher. Seed
        # its current lifecycle from JSONL; finished is a state now, not a
        # dismissible notification.
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
                if inferred == "working":
                    changed = bg_status.update_status(user.id, old_sess.id, "working")
                elif (
                    inferred == "finished"
                    and bg_status.get_status(user.id, old_sess.id) is None
                ):
                    # Legacy/migration fallback only. A tracked ``finished``
                    # stays unread until its own carrier/session is tapped.
                    changed = bg_status.update_status(
                        user.id, old_sess.id, "seen_finished"
                    )
                if inferred == "working":
                    try:
                        from ...usage import context_pct_for_session

                        pct = await context_pct_for_session(old_sess)
                    except Exception as e:
                        logger.debug("infer bg context failed: %s", e)
                        pct = None
                    if pct is not None:
                        bg_status.set_context_pct(user.id, old_sess.id, pct)
                        changed = True
                if changed:
                    try:
                        from ...handlers.notifications import refresh_panel

                        await refresh_panel(context.bot, user.id)
                    except Exception as e:
                        logger.debug("refresh_panel after seed failed: %s", e)

            _asyncio.create_task(_seed_bg_status(old_active))

        # The switcher tap always lands the user on the session's
        # history view, regardless of which view fired it (main card,
        # history and other card views). The Menu button anchored to the bottom row
        # keeps the layout visually stable across the transition.

        # If this bg session has a stashed AskUserQuestion / ExitPlanMode /
        # permission prompt, paint kb-mode on the carrier directly.
        # Re-verify against the live pane first — claude may have moved
        # on while the badge was up.
        showed_interactive_ui = False
        pending_ui = bg_status.get_pending_interactive_ui(user.id, target_id)
        if pending_ui is not None and sess.window_id and query.message is not None:
            pane = await capture_window(sess.window_id)
            if pane and is_interactive_ui(pane):
                content_obj = extract_interactive_content(pane)
                if content_obj is not None:
                    # Claim the carrier as the live card msg, then
                    # flip it into kb-mode view. paint_card_on_carrier
                    # sets msg_id; enter_kb_mode then edits in place.
                    try:
                        state = get_card_state(user.id, sess)
                        bind_carrier(
                            state,
                            query.message.message_id,
                            CarrierKind.TEXT,
                        )
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
                    cached_file_id = getattr(sess, "screenshot_file_id", "")
                    cached_at = float(getattr(sess, "screenshot_cached_at", 0.0))
                    used_cached_screenshot = bool(
                        cached_file_id
                        and get_card_state(user.id, sess).rich_media_file_id
                        == cached_file_id
                    )
                    await paint_card_on_carrier(
                        context.bot,
                        user.id,
                        sess,
                        query.message.message_id,
                        refresh_pane=not used_cached_screenshot,
                    )
                    painted = True
                    if (
                        used_cached_screenshot
                        and time.time() - cached_at > SCREENSHOT_CACHE_FRESH_SECONDS
                    ):
                        import asyncio as _asyncio

                        _asyncio.create_task(
                            refresh_cached_screenshot(
                                context.bot,
                                user.id,
                                sess,
                                query.message.message_id,
                            )
                        )
                except Exception as e:
                    logger.debug("paint_card_on_carrier failed: %s", e)
            if not painted:
                try:
                    preview = await render_session_preview(sess)
                    await safe_edit(query, preview)
                except Exception as e:
                    logger.debug("preview safe_edit failed: %s", e)

        # NB: do NOT call refresh_panel here. The carrier message just
        # got painted with the history view (or the pending interactive
        # UI). refresh_panel re-renders the live card on the same
        # message_id with the fresh-session state, whose ``lines`` is
        # empty — overwriting the history we just put there with a
        # header-only card. The next real claude event for the active
        # session will re-render the card naturally; the panel update
        # arrives with it.

        return True

    if data == CB_SW_NEW:
        # The entire + new flow lives on the SAME carrier message: old
        # live card → dir browser → new session's empty live card. No
        # extra "Created" notice — the message just transitions in place.
        #
        # Pause the active session first so its events buffer silently
        # while the user picks a directory; events catch up when the
        # user switches back via the switcher.
        from ...startup_queue import begin_startup_queue

        begin_startup_queue(user.id)
        active = session_manager.get_active_session(user.id)
        if active is not None:
            pause_card_view(user.id, active.id)
        from .dir_browser import open_new_session_flow

        try:
            await open_new_session_flow(query, context, user.id, origin="main")
        except Exception:
            logger.exception("Could not open new-session flow")
            await safe_send(context.bot, user.id, "Could not open new-session flow")
        await query.answer()
        return True

    return False
