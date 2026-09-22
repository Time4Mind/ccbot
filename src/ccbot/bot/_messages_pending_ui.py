"""Pending terminal-UI guards for inbound Telegram messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot

from ..handlers.interactive_ui import handle_interactive_ui
from ..handlers.message_sender import safe_reply
from ..handlers.notifications import enter_kb_mode
from ..session import session_manager
from ..terminal_parser import extract_interactive_content, is_interactive_ui
from ..terminal_runtime import PaneCaptureError, capture_window_pane
from ..tmux_manager import tmux_manager
from ..transfer_runtime import get_node_runtime

logger = logging.getLogger(__name__)


async def _pane_has_interactive_ui(wid: str) -> bool:
    """Return whether the owner node currently shows an interactive prompt."""
    try:
        pane_text = await capture_window_pane(
            wid,
            manager=session_manager,
            tmux=tmux_manager,
            runtime_getter=get_node_runtime,
        )
    except PaneCaptureError:
        return False
    return bool(pane_text) and is_interactive_ui(pane_text)


async def _intercept_if_pending_ui(
    bot: Bot,
    user_id: int,
    wid: str,
    reply_to: Any,
    wasnt_sent_notice: str | None = None,
    *,
    wait_until_clear: bool = False,
) -> bool:
    """Surface a pending prompt before it can consume ordinary input."""
    surfaced = False
    while True:
        try:
            pane_text = await capture_window_pane(
                wid,
                manager=session_manager,
                tmux=tmux_manager,
                runtime_getter=get_node_runtime,
            )
        except PaneCaptureError as exc:
            await safe_reply(
                reply_to,
                f"❌ Could not verify the session terminal: {exc}. "
                "Your message wasn't sent.",
            )
            return True
        if not pane_text or not is_interactive_ui(pane_text):
            if surfaced and wait_until_clear:
                logger.info(
                    "pending_ui_cleared_resuming_inbound user=%d wid=%s",
                    user_id,
                    wid,
                )
            return False
        if not surfaced:
            sess = session_manager.find_session_by_window(wid)
            active = session_manager.get_active_session(user_id)
            is_active = sess is not None and active is not None and active.id == sess.id
            if is_active and sess is not None:
                content_obj = extract_interactive_content(pane_text)
                if content_obj is not None:
                    await enter_kb_mode(
                        bot, user_id, sess, content_obj.content, content_obj.name
                    )
                    surfaced = True
            if not surfaced:
                await handle_interactive_ui(bot, user_id, wid)
                surfaced = True
        if wait_until_clear:
            await asyncio.sleep(0.25)
            continue
        break
    logger.info(
        "intercepted_user_msg_pending_ui user=%d wid=%s",
        user_id,
        wid,
        extra={
            "event": "intercepted_user_msg_pending_ui",
            "user_id": user_id,
            "window_id": wid,
        },
    )
    try:
        await safe_reply(
            reply_to,
            wasnt_sent_notice
            or (
                "⏳ Pending prompt above — answer it via the keyboard first. "
                "Your message wasn't sent."
            ),
        )
    except Exception:
        pass
    return True


__all__ = ["_intercept_if_pending_ui", "_pane_has_interactive_ui"]
