"""Route explicit model/mode buttons without exposing terminal navigation."""

from __future__ import annotations

from typing import Any

from telegram.error import TelegramError

from ... import opencode_picker as picker_ui
from ...session import session_manager
from ...terminal_runtime import PaneCaptureError, capture_session_pane, send_session_key
from ...opencode_runtime import interactive_panel


async def handle(query: Any, context: Any, user: Any) -> bool:
    data = query.data or ""
    if not data.startswith((picker_ui.PREFIX, picker_ui.CANCEL, picker_ui.PAGE)):
        return False
    _, action, token, *extra = data.split(":")
    picker = picker_ui.resolve(user.id, token)
    sess = session_manager.get_active_session(user.id)
    if (
        picker is None
        or sess is None
        or sess.id != picker.session_id
        or sess.window_id != picker.window_id
        or sess.backend != "opencode"
    ):
        await query.answer("Этот селектор устарел. Откройте /model заново.")
        return True
    await query.answer()
    from .interactive_ui import refresh_after_key

    pane_after_choice = None
    try:
        if action == "pick":
            if len(extra) != 1 or not extra[0].isdigit():
                return True
            pane_after_choice = await picker_ui.choose(
                picker, sess, int(extra[0]), token
            )
        elif action == "page":
            if len(extra) != 1 or not extra[0].isdigit():
                return True
            page = int(extra[0])
            if 0 <= page * picker_ui.PAGE_SIZE < len(picker.labels):
                picker.page = page
        else:
            async with picker.lock:
                if (
                    picker.token != token
                    or picker_ui.resolve(user.id, token) is not picker
                ):
                    return True
                pane = await capture_session_pane(sess)
                panel = interactive_panel(pane)
                if panel and panel[0] in ("Settings", "OpenCodeVariant"):
                    await send_session_key(sess, "Escape")
                picker_ui.discard(user.id, sess.window_id)
    except (OSError, ValueError, TimeoutError, PaneCaptureError) as exc:
        try:
            await context.bot.send_message(chat_id=user.id, text=str(exc))
        except TelegramError:
            pass
    await refresh_after_key(
        context.bot, user.id, sess.window_id, pane_text=pane_after_choice
    )
    return True
