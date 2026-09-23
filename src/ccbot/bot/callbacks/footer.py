"""Footer callbacks for the live-card controls and inline Options row."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_CONF_CLEAR_NO,
    CB_CONF_CLEAR_YES,
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
    CB_FT_CLEAR,
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_OPTIONS,
    CB_FT_SCREENSHOT,
    CB_FT_STOP,
    CB_FT_TERM,
    CB_KB_BACK,
    CB_KB_RESUME,
    CB_PG_JUMP,
    CB_PG_NEXT,
    CB_PG_PREV,
)
from ...handlers.card_model import TurnPhase
from ...handlers.message_sender import safe_send
from ...handlers.menu import (
    build_footer_keyboard,
    toggle_footer_options,
)
from ...handlers.notifications import (
    _card_is_busy,
    card_page_info,
    enter_kb_mode,
    exit_kb_mode,
    get_card_state,
    pause_card_view,
    refresh_panel,
)
from ...i18n import t
from ...session import session_manager
from ...terminal_runtime import capture_session_pane, send_session_key
from .._common import set_view

logger = logging.getLogger(__name__)
_screenshot_tasks: dict[int, asyncio.Task[None]] = {}


async def _apply_screenshot_toggle(bot: Any, user_id: int, enabled: bool) -> None:
    """Converge screenshot state outside Telegram's serialized callback path."""
    try:
        sess = session_manager.get_active_session(user_id)
        if enabled and sess is not None and sess.node_id != "local":
            await capture_session_pane(sess)
        refreshed = await refresh_panel(
            bot, user_id, immediate=True, refresh_keyboard=True
        )
        if enabled and sess is not None and sess.node_id != "local":
            state = get_card_state(user_id, sess)
            if not refreshed or not state.is_rich_media_msg:
                raise RuntimeError("remote screenshot could not update the card")
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        current = session_manager.get_user_settings(user_id)
        if enabled and current.get("card_inline_screenshots", False):
            session_manager.update_user_setting(
                user_id, "card_inline_screenshots", False
            )
            try:
                await refresh_panel(bot, user_id, immediate=True, refresh_keyboard=True)
            except Exception:
                logger.debug("Screenshot rollback repaint failed", exc_info=True)
        await safe_send(bot, user_id, t(user_id, "toast.screenshot_failed", msg=detail))


def _screenshot_done(user_id: int, task: asyncio.Task[None]) -> None:
    if _screenshot_tasks.get(user_id) is task:
        _screenshot_tasks.pop(user_id, None)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Screenshot toggle task failed user_id=%s", user_id)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data == CB_FT_STOP:
        sess = session_manager.get_active_session(user.id)
        if sess is None or not sess.window_id:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        if not await send_session_key(sess, "Escape"):
            await query.answer(t(user.id, "toast.window_gone"), show_alert=False)
            return True
        # Stop is an authoritative user transition. A stale pane spinner or
        # late transcript event from the interrupted turn must not flip the
        # button back to Stop and trap the session in an unclosable state.
        if sess.window_id:
            state = get_card_state(user.id, sess)
            state.user_stopped = True
            state.pane_busy = False
            state.pane_status = ""
            state.turn_phase = TurnPhase.IDLE
            state.stall_watch_active = False
        await query.answer(t(user.id, "toast.esc_sent"))
        await refresh_panel(context.bot, user.id, immediate=True, refresh_keyboard=True)
        return True

    if data == CB_FT_KILL:
        sess = session_manager.get_active_session(user.id)
        if sess is None or sess.state not in ("active", "idle", "lost"):
            await query.answer(t(user.id, "toast.nothing_to_kill"), show_alert=False)
            return True
        # Pause the card so live updates don't repaint over the
        # confirmation prompt. Resumed on Yes (archive resets card) or
        # No (CB_CONF_KILL_NO handler clears the pause + re-renders).
        pause_card_view(user.id, sess.id)
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
        # Clear has no rollback (unlike Kill → Restore the archived
        # window). Surface a confirmation dialog like /kill so a stray
        # tap on a phone doesn't nuke session context. The Yes branch
        # also chains Stop (Esc) before /clear so the latter lands on
        # a clean prompt — previously /clear-while-busy silently
        # failed inside claude.
        sess = session_manager.get_active_session(user.id)
        if sess is None or sess.state not in ("active", "idle"):
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        # Pause so live updates don't repaint over the prompt.
        pause_card_view(user.id, sess.id)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user.id, "btn.yes_clear"),
                        callback_data=f"{CB_CONF_CLEAR_YES}{sess.id}"[:64],
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.no"), callback_data=CB_CONF_CLEAR_NO
                    ),
                ]
            ]
        )
        await set_view(
            query, context.bot, user.id, t(user.id, "conf.clear", name=sess.name), kb
        )
        await query.answer()
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
        from .more_menu import begin_menu_refresh, render_menu_text

        text = render_menu_text(user.id, refreshing=True)
        keyboard = build_footer_keyboard(user.id, screen="more")
        target = await set_view(query, context.bot, user.id, text, keyboard)
        begin_menu_refresh(target or query, user.id)
        await query.answer()
        return True

    if data == CB_FT_OPTIONS:
        toggle_footer_options(user.id)
        await query.answer()
        await refresh_panel(context.bot, user.id, immediate=True, refresh_keyboard=True)
        return True

    if data == CB_FT_SCREENSHOT:
        settings = session_manager.get_user_settings(user.id)
        enabled = not bool(settings.get("card_inline_screenshots", False))
        session_manager.update_user_setting(user.id, "card_inline_screenshots", enabled)
        await query.answer()
        task = asyncio.create_task(
            _apply_screenshot_toggle(context.bot, user.id, enabled),
            name=f"screenshot-toggle-{user.id}",
        )
        _screenshot_tasks[user.id] = task
        task.add_done_callback(lambda done, uid=user.id: _screenshot_done(uid, done))
        return True

    if data in (CB_PG_PREV, CB_PG_NEXT, CB_PG_JUMP):
        sess = session_manager.get_active_session(user.id)
        if sess is None:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        from .._new_session_flow import cancel_for_active_card

        flow_cancelled = cancel_for_active_card(
            user.id, getattr(context, "user_data", None)
        )
        # Stop Telegram's button spinner before rendering or making the edit
        # request. An expired answer must not prevent the actual page paint.
        try:
            await query.answer()
        except Exception as e:
            logger.debug("pagination callback answer failed: %s", e)
        state = get_card_state(user.id, sess)
        if flow_cancelled:
            # Opening Start pauses the old card. Pagination winning the race
            # makes that card live again, including its normal input routing.
            state.in_menu_view = False
        idx, total = card_page_info(state, user.id)
        old_page_idx = state.current_page_idx
        if data == CB_PG_JUMP:
            # Jump to default-focus (= latest page when no answer-anchor
            # was set explicitly). ``None`` means "stick to latest".
            state.current_page_idx = None
        elif data == CB_PG_PREV:
            state.current_page_idx = (idx - 1) % total
        else:  # CB_PG_NEXT
            state.current_page_idx = (idx + 1) % total
        desired_page_idx = state.current_page_idx
        refreshed = False
        try:
            refreshed = await refresh_panel(context.bot, user.id, immediate=True)
        except Exception as e:
            logger.debug("pagination refresh failed: %s", e)
        if not refreshed and state.current_page_idx == desired_page_idx:
            state.current_page_idx = old_page_idx
        return True

    if data == CB_KB_BACK:
        # User taps Back from kb-mode → flip card back to regular view.
        # Pending kept (kb_prompt stays); Resume button shows in footer
        # to allow re-entry. Auto-clear happens later if claude moves
        # past the prompt (handled by session_events).
        sess = session_manager.get_active_session(user.id)
        if sess is None:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        await exit_kb_mode(context.bot, user.id, sess, clear_pending=False)
        await query.answer()
        return True

    if data == CB_KB_RESUME:
        # User taps Resume → flip back into kb-mode using stored prompt.
        sess = session_manager.get_active_session(user.id)
        if sess is None:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        state = get_card_state(user.id, sess)
        if not state.kb_prompt:
            await query.answer("No pending action", show_alert=False)
            return True
        await enter_kb_mode(
            context.bot, user.id, sess, state.kb_prompt, state.kb_ui_name
        )
        await query.answer()
        return True

    if data == CB_FT_TERM:
        # The terminal action is disclosed by Options. A stale tap after the
        # row collapses still opens the active tmux window safely.
        from ...local_terminal import open_terminal_for_window

        sess = session_manager.get_active_session(user.id)
        if sess is None or not sess.window_id:
            await query.answer(t(user.id, "toast.no_session"), show_alert=False)
            return True
        if getattr(sess, "node_id", "local") != "local":
            await query.answer(
                "Terminal is available only for sessions on this machine.",
                show_alert=True,
            )
            return True
        await open_terminal_for_window(sess.window_id, user_id=user.id)
        await query.answer(t(user.id, "toast.term_opened"))
        # Refresh the footer keyboard on the current message so the Term
        # button drops out (next render will see the attached client).
        active = session_manager.get_active_session(user.id)
        is_busy = False
        if active is not None:
            is_busy = _card_is_busy(get_card_state(user.id, active))
        keyboard = build_footer_keyboard(user.id, screen="main", is_busy=is_busy)
        if keyboard is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception as e:
                logger.debug("term post-spawn keyboard refresh failed: %s", e)
        return True

    return False
