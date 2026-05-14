"""Screenshot-message callbacks (CB_SCREENSHOT_REFRESH / CB_KEYS_* /
CB_SHOT_SW / CB_SHOT_BACK / CB_SHOT_MODE / CB_SHOT_KEYS).

Two flavours of screenshot live in the chat:

* ``/screenshot`` slash → reply_document with the arrow-key control
  keyboard. Handled by ``CB_SCREENSHOT_REFRESH`` + ``CB_KEYS_*``.
* Shot button (main footer / /list view) → ``emit_screenshot_compact``
  sends a photo with either a switcher-mode keyboard (default) or a
  kb-mode key grid. Handled by ``CB_SHOT_SW`` (switch active +
  redraw), ``CB_SHOT_BACK`` (delete photo + restore origin surface),
  ``CB_SHOT_MODE`` (toggle switcher ↔ kb-mode + refresh photo), and
  ``CB_SHOT_KEYS`` (kb-mode key press: send_keys + refresh photo).
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from telegram import InputMediaDocument, InputMediaPhoto
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_SHOT_BACK,
    CB_SHOT_KEYS,
    CB_SHOT_MODE,
    CB_SHOT_SW,
)
from ...handlers import bg_status
from ...handlers.history import send_history
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_send
from ...screenshot import text_to_image
from ...session import session_manager
from ...tmux_manager import tmux_manager
from ..commands.info import build_screenshot_compact_keyboard, build_screenshot_keyboard

logger = logging.getLogger(__name__)


# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data.startswith(CB_SHOT_SW):
        target_id = data[len(CB_SHOT_SW) :]
        sess = session_manager.get_session(target_id)
        if sess is None or sess.state not in ("active", "idle") or not sess.window_id:
            await query.answer("Session not available", show_alert=True)
            return True
        # Origin sticks to whatever surface the photo was opened from.
        # The Back button on the rebuilt keyboard preserves it.
        origin = "m"
        if query.message and query.message.reply_markup:
            for row in query.message.reply_markup.inline_keyboard:
                for btn in row:
                    cb = btn.callback_data or ""
                    if cb.startswith(CB_SHOT_BACK):
                        origin = cb[len(CB_SHOT_BACK) :] or "m"
                        break

        session_manager.set_active_session(user.id, target_id)
        bg_status.mark_seen(user.id, target_id)
        bg_status.prune_seen(user.id)

        w = await tmux_manager.find_window_by_id(sess.window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return True
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return True
        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = build_screenshot_compact_keyboard(user.id, origin)
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(media=io.BytesIO(png_bytes)),
                reply_markup=keyboard,
            )
            await query.answer(f"→ {sess.name or sess.id}")
        except Exception as e:
            logger.debug("shot switch edit failed: %s", e)
            await query.answer("Refresh failed", show_alert=True)
        return True

    if data.startswith(CB_SHOT_BACK):
        origin = data[len(CB_SHOT_BACK) :] or "m"
        if query.message:
            try:
                await query.message.delete()
            except Exception as e:
                logger.debug("shot back: delete photo failed: %s", e)
        active_sess = session_manager.get_active_session(user.id)
        from .more_menu import HISTORY_ORIGIN_KEY, build_list_view

        if origin == "l":
            # Re-emit the /list view as a fresh message: history of the
            # active session + the standard /list footer underneath.
            body, kb = build_list_view(user.id)
            if active_sess is None or not active_sess.window_id:
                await safe_send(context.bot, user.id, body, reply_markup=kb)
            else:
                if context.user_data is not None:
                    context.user_data[HISTORY_ORIGIN_KEY] = "menu_list"
                extra_rows = [list(r) for r in kb.inline_keyboard]
                try:
                    await send_history(
                        target=None,
                        window_id=active_sess.window_id,
                        edit=False,
                        user_id=user.id,
                        bot=context.bot,
                        extra_rows=extra_rows,
                    )
                except Exception as e:
                    logger.debug("shot back: fresh /list paint failed: %s", e)
                    await safe_send(context.bot, user.id, body, reply_markup=kb)
        else:
            # origin == "m" — send the active session's history with the
            # main-screen footer. Mirrors /list's Back path so the user
            # lands on the paginated transcript instead of an empty card.
            if active_sess is not None and active_sess.window_id:
                if context.user_data is not None:
                    context.user_data[HISTORY_ORIGIN_KEY] = "switcher"
                footer_kb = build_footer_keyboard(
                    user.id,
                    screen="main",
                    is_busy=False,
                    include_older_btn=False,
                )
                extra_rows = (
                    [list(r) for r in footer_kb.inline_keyboard]
                    if footer_kb is not None
                    else None
                )
                try:
                    await send_history(
                        target=None,
                        window_id=active_sess.window_id,
                        edit=False,
                        user_id=user.id,
                        bot=context.bot,
                        extra_rows=extra_rows,
                    )
                except Exception as e:
                    logger.debug("shot back: history paint failed: %s", e)
        await query.answer()
        return True

    if data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return True

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return True

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error("Failed to refresh screenshot: %s", e)
            await query.answer("Failed to refresh", show_alert=True)
        return True

    if data.startswith(CB_SHOT_MODE):
        # sh:m:<k|s>:<m|l> — toggle the compact photo's keyboard between
        # switcher rows and an arrow / Enter / Esc grid. Both modes also
        # refresh the photo so the user sees the current pane state.
        rest = data[len(CB_SHOT_MODE) :]
        parts = rest.split(":", 1)
        if len(parts) != 2 or parts[0] not in ("k", "s"):
            await query.answer("Invalid data")
            return True
        mode, origin = parts
        active = session_manager.get_active_session(user.id)
        if active is None or not active.window_id:
            await query.answer("No active session", show_alert=True)
            return True
        w = await tmux_manager.find_window_by_id(active.window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return True
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return True
        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = build_screenshot_compact_keyboard(user.id, origin, mode=mode)
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(media=io.BytesIO(png_bytes)),
                reply_markup=keyboard,
            )
            await query.answer("⌨" if mode == "k" else "switcher")
        except Exception as e:
            logger.debug("shot mode toggle failed: %s", e)
            await query.answer("Refresh failed", show_alert=True)
        return True

    if data.startswith(CB_SHOT_KEYS):
        # shk:<key_id>:<origin>:<window_id> — kb-mode key press in the
        # compact photo view. Sends the key into the named window, then
        # refreshes the photo and keeps the kb-mode keyboard attached.
        rest = data[len(CB_SHOT_KEYS) :]
        parts = rest.split(":", 2)
        if len(parts) != 3:
            await query.answer("Invalid data")
            return True
        key_id, origin, window_id = parts
        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return True
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return True
        tmux_key, enter, literal = key_info
        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))
        # Let the pane render its reaction before we capture.
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = build_screenshot_compact_keyboard(user.id, origin, mode="k")
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=io.BytesIO(png_bytes)),
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.debug("shot keys refresh failed: %s", e)
        return True

    if data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return True
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return True

        tmux_key, enter, literal = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return True

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))

        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # screenshot unchanged or message too old
        return True

    return False
