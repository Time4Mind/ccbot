"""Screenshot-message callbacks (CB_SCREENSHOT_REFRESH / CB_KEYS_* /
CB_SHOT_SW / CB_SHOT_BACK / CB_SHOT_MODE / CB_SHOT_KEYS).

Two flavours of screenshot live in the chat:

* ``/screenshot`` slash → reply_document with the arrow-key control
  keyboard. Handled by ``CB_SCREENSHOT_REFRESH`` + ``CB_KEYS_*``.
* Shot button (main footer top row) → ``emit_screenshot_compact``
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
    CB_SHOT_SW,
)
from ...handlers import bg_status
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
        bg_status.clear_for_user_session(user.id, target_id)
        # Hold the new active session's card in menu-mode so claude
        # events buffer silently while the user is still on the photo.
        # The Back handler's ``resume_card_view`` clears this flag and
        # spawns / repaints the card afterwards. Without this, events
        # for the newly-active session would race the photo view
        # because ``_should_buffer`` no longer treats it as bg.
        from ...handlers.notifications import mark_card_paused

        mark_card_paused(user.id, target_id)

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
        # Delete the photo, resume the paused live card. Events that
        # arrived during the screenshot view buffered into state.events;
        # resume_card_view renders them into the existing carrier so the
        # user lands back on the same card slot with the updated body.
        # If the carrier was lost (5-min stale window, archived), the
        # next claude event spawns a fresh one — but we don't repost
        # here proactively to avoid a stray new card sitting in chat.
        if query.message:
            try:
                await query.message.delete()
            except Exception as e:
                logger.debug("shot back: delete photo failed: %s", e)
        active_sess = session_manager.get_active_session(user.id)
        if active_sess is not None and active_sess.window_id:
            from ...handlers.notifications import resume_card_view

            try:
                await resume_card_view(context.bot, user.id, active_sess)
            except Exception as e:
                logger.debug("shot back: resume_card_view failed: %s", e)
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
