"""Interactive-UI navigation (CB_ASK_*).

Up / Down / Left / Right / Esc / Enter / Space / Tab / Refresh — drive
Claude's native interactive prompts (AskUserQuestion / ExitPlanMode /
permission menus) from a Telegram inline keyboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from ...handlers.interactive_ui import clear_interactive_msg, handle_interactive_ui
from ...handlers.kb_mode import parse_picker_menu
from ...handlers.notifications import enter_kb_mode, exit_kb_mode, has_pending_kb
from ...session import session_manager
from ...session_models import Session
from ...terminal_parser import (
    InteractiveUIContent,
    extract_interactive_content,
    is_interactive_ui,
)
from ...tmux_input_transport import send_control_keys
from ...tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


_NAV = (
    (CB_ASK_UP, "Up", "↑"),
    (CB_ASK_DOWN, "Down", "↓"),
    (CB_ASK_LEFT, "Left", "←"),
    (CB_ASK_RIGHT, "Right", "→"),
    (CB_ASK_ENTER, "Enter", "⏎ Enter"),
    (CB_ASK_SPACE, "Space", "␣ Space"),
    (CB_ASK_TAB, "Tab", "⇥ Tab"),
)


async def capture_window(window_id: str) -> str | None:
    sess = session_manager.find_session_by_window(window_id)
    if sess is not None:
        from ...terminal_runtime import capture_session_pane

        try:
            return await capture_session_pane(sess)
        except Exception:
            logger.exception("Pane capture failed for %s", sess.id)
            return None
    w = await tmux_manager.find_window_by_id(window_id)
    return await tmux_manager.capture_pane(w.window_id) if w is not None else None


async def _send_window_key(window_id: str, key: str) -> bool:
    sess = session_manager.find_session_by_window(window_id)
    if sess is not None:
        from ...terminal_runtime import send_session_key

        return await send_session_key(sess, key)
    w = await tmux_manager.find_window_by_id(window_id)
    if w is None:
        return False
    return await tmux_manager.send_keys(w.window_id, key, enter=False, literal=False)


async def confirm_interactive_selection(bot: Any, user_id: int, window_id: str) -> bool:
    """Press Enter in a session's native picker and refresh its Telegram controls."""
    sess = session_manager.find_session_by_window(window_id)
    previous = None
    if sess is not None and sess.backend == "codex":
        content = extract_interactive_content(await capture_window(window_id) or "")
        if content and parse_picker_menu(content.content, content.name):
            previous = content
    if not await _send_window_key(window_id, "Enter"):
        return False
    await _refresh_after_selection(bot, user_id, window_id, previous)
    return True


async def _wait_for_picker_frame(
    window_id: str, previous: InteractiveUIContent | None = None
) -> tuple[str, InteractiveUIContent | None] | None:
    """Observe a fresh native picker frame without waiting for the next status tick."""
    previous_menu = _picker_options(previous) if previous else None
    no_dialog_frames = 0
    try:
        async with asyncio.timeout(0.65):
            while True:
                pane = await capture_window(window_id)
                if pane:
                    content = extract_interactive_content(pane)
                    if previous is None:
                        if content and (
                            content.name == "OpenCodeVariant"
                            or parse_picker_menu(content.content, content.name)
                            or (
                                content.name == "Settings"
                                and "Select model" in content.content
                                and "ctrl+a" in content.content
                            )
                        ):
                            return pane, content
                    elif (
                        content is not None
                        and _picker_options(content) != previous_menu
                    ):
                        return pane, content
                    elif content is None:
                        no_dialog_frames += 1
                        if no_dialog_frames >= 2:
                            return pane, None
                    else:
                        no_dialog_frames = 0
                else:
                    no_dialog_frames = 0
                await asyncio.sleep(0.04)
    except TimeoutError:
        return None


def _picker_options(content: InteractiveUIContent) -> tuple[str, ...]:
    picker = parse_picker_menu(content.content, content.name)
    title = content.content.partition("\n")[0].strip()
    return (
        (content.name, title, *(option.label for option in picker.options))
        if picker
        else (content.name, title)
    )


async def _refresh_after_selection(
    bot: Any, user_id: int, window_id: str, previous: InteractiveUIContent | None
) -> None:
    if previous is None:
        await asyncio.sleep(0.5)
    else:
        frame = await _wait_for_picker_frame(window_id, previous=previous)
        if frame is not None:
            await _refresh_after_key(bot, user_id, window_id, pane_text=frame[0])
            return
    await _refresh_after_key(bot, user_id, window_id)


async def present_native_picker(bot: Any, user_id: int, sess: Session) -> bool:
    """Use the native dialog as proof that the slash command was submitted."""
    frame = await _wait_for_picker_frame(sess.window_id)
    if frame is None or frame[1] is None:
        return False
    active = session_manager.get_active_session(user_id)
    if active is not None and active.id == sess.id:
        content = frame[1]
        try:
            await enter_kb_mode(bot, user_id, sess, content.content, content.name)
        except TelegramError:
            logger.debug("Native picker refresh failed for %s", sess.id, exc_info=True)
    return True


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    from .opencode_picker import handle as handle_opencode_picker

    if await handle_opencode_picker(query, context, user):
        return True
    data = query.data or ""

    if data.startswith(CB_ASK_PICK):
        payload = data[len(CB_ASK_PICK) :]
        target_raw, separator, window_id = payload.partition(":")
        if not separator or not target_raw.isdigit() or not window_id:
            await query.answer()
            return True
        target = int(target_raw)
        await query.answer()
        pane = await capture_window(window_id)
        if pane is not None:
            content = extract_interactive_content(pane or "")
            picker = (
                parse_picker_menu(content.content, content.name)
                if content is not None
                else None
            )
            if picker is not None and 0 <= target < len(picker.options):
                delta = target - picker.cursor_index
                key = "Down" if delta > 0 else "Up"
                sess = session_manager.find_session_by_window(window_id)
                codex = sess is not None and sess.backend == "codex"
                if codex and getattr(sess, "node_id", None) == "local":
                    sent = await send_control_keys(
                        window_id, *([key] * abs(delta)), "Enter"
                    )
                else:
                    for _ in range(abs(delta)):
                        await _send_window_key(window_id, key)
                    sent = await _send_window_key(window_id, "Enter")
                if sent:
                    previous = content if codex else None
                    await _refresh_after_selection(
                        context.bot, user.id, window_id, previous
                    )
        return True

    for prefix, tmux_key, toast in _NAV:
        if data.startswith(prefix):
            window_id = data[len(prefix) :]
            await query.answer(
                toast if prefix in (CB_ASK_ENTER, CB_ASK_SPACE, CB_ASK_TAB) else ""
            )
            if tmux_key == "Enter":
                await confirm_interactive_selection(context.bot, user.id, window_id)
            elif await _send_window_key(window_id, tmux_key):
                await asyncio.sleep(0.5)
                await _refresh_after_key(context.bot, user.id, window_id)
            return True

    if data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        await query.answer("⎋ Esc")
        if await _send_window_key(window_id, "Escape"):
            # Floating-msg flow tracks an explicit msg-per-window;
            # card-based kb-mode is cleared by exit_kb_mode. Cover both.
            await clear_interactive_msg(user.id, context.bot, window_id)
            sess = session_manager.find_session_by_window(window_id)
            if sess is not None:
                has_prompt, in_kb = has_pending_kb(user.id, sess.id)
                if has_prompt or in_kb:
                    await exit_kb_mode(context.bot, user.id, sess, clear_pending=True)
        return True

    if data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        await query.answer("🔄")
        await _refresh_after_key(context.bot, user.id, window_id)
        return True

    return False


async def refresh_after_key(
    bot: Any, user_id: int, window_id: str, *, pane_text: str | None = None
) -> None:
    """Refresh terminal-driven controls after a backend-specific choice."""
    await _refresh_after_key(bot, user_id, window_id, pane_text=pane_text)


async def _refresh_after_key(
    bot: Any, user_id: int, window_id: str, *, pane_text: str | None = None
) -> None:
    """Re-paint the interactive surface after a key press.

    Active session in kb-mode → re-enter kb-mode on the card with the
    new pane snapshot (1s status_polling lag would otherwise leave the
    keyboard stale). Otherwise fall back to the floating-msg flow.
    """
    sess = session_manager.find_session_by_window(window_id)
    if sess is None:
        await handle_interactive_ui(bot, user_id, window_id)
        return
    active = session_manager.get_active_session(user_id)
    if active is not None and active.id == sess.id:
        # Card-based kb-mode for the active session: capture pane, if
        # the UI is still up re-enter; if it cleared, drop kb-mode.
        pane = pane_text if pane_text is not None else await capture_window(window_id)
        if pane and is_interactive_ui(pane):
            content_obj = extract_interactive_content(pane)
            if content_obj is not None:
                await enter_kb_mode(
                    bot, user_id, sess, content_obj.content, content_obj.name
                )
                return
        has_prompt, in_kb = has_pending_kb(user_id, sess.id)
        if has_prompt or in_kb:
            await exit_kb_mode(bot, user_id, sess, clear_pending=True)
        return
    await handle_interactive_ui(bot, user_id, window_id)
