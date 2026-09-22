"""Bash capture and reply-quote helpers for Telegram text routing."""

from __future__ import annotations

import asyncio

from telegram import Bot, Update

from ..handlers.card_types import TurnPhase
from ..handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_reply,
    send_with_fallback,
    try_rich_edit,
)
from ..handlers.notifications import get_card_state, lookup_session_for_message
from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..terminal_parser import extract_bash_output
from ..tmux_manager import tmux_manager
from ..transfer_runtime import get_node_runtime
from ..terminal_runtime import (
    PaneCaptureError,
    capture_window_pane,
    session_is_reachable,
)


# Active bash capture tasks: (user_id, window_id) → asyncio.Task
bash_capture_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}


def cancel_bash_capture(user_id: int, window_id: str) -> None:
    """Cancel any running bash capture for this (user, window) pair."""
    key = (user_id, window_id)
    task = bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def capture_bash_output(
    bot: Bot, user_id: int, window_id: str, command: str
) -> None:
    """Background task: capture ``!cmd`` output from the pane and surface it.

    Sends the first non-empty capture as a new message, then edits in place
    as more output appears. Stops after 30 ticks (~30 s) or on cancel.
    """
    try:
        await asyncio.sleep(2.0)
        chat_id = user_id
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            try:
                raw = await capture_window_pane(
                    window_id,
                    manager=session_manager,
                    tmux=tmux_manager,
                    runtime_getter=get_node_runtime,
                )
            except PaneCaptureError:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue
            if output == last_output:
                await asyncio.sleep(1.0)
                continue
            last_output = output

            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                sent = await send_with_fallback(bot, chat_id, output)
                if sent:
                    msg_id = sent.message_id
            # Rich-first so in-place edits keep the same rendering as the
            # initial send (which goes rich via send_with_fallback).
            elif not await try_rich_edit(bot, chat_id, msg_id, output):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        bash_capture_tasks.pop((user_id, window_id), None)


async def route_reply_quote(update: Update, user_id: int, text: str) -> bool:
    """Reply-quote routing: if the user replied to a bot message that
    belongs to a non-active session, send this single message there
    without changing the active session pointer.

    Returns True iff the message was fully handled and ``text_handler``
    must ``return`` (sent to the quoted session, send error, or quoted
    message has no session). Returns False to fall through to the
    active-session dispatch — both when there is no reply-quote at all
    and when the quoted session is dead (a warning is emitted first).
    """
    assert update.message is not None
    reply = update.message.reply_to_message
    if reply is None:
        return False
    target_sid = lookup_session_for_message(user_id, reply.message_id)
    if not target_sid:
        return False
    target = session_manager.get_session(target_sid)
    active_sess = session_manager.get_active_session(user_id)
    same_as_active = active_sess is not None and active_sess.id == target_sid
    if (
        target is not None
        and target.window_id
        and target.state in ("active", "idle")
        and not same_as_active
    ):
        if await session_is_reachable(
            target, tmux=tmux_manager, runtime_getter=get_node_runtime
        ):
            ok, sm = await session_manager.send_to_window(target.window_id, text)
            if ok:
                session_manager.touch_session(target.id)
                get_card_state(user_id, target).turn_phase = TurnPhase.RUNNING
                # Explicit feedback so the user can see which
                # session received the reply-quote — bg session
                # would otherwise stay silent until the next
                # carrier interaction.
                await safe_reply(
                    update.message,
                    f"↩ \\[{target.name or target.id}\\]",
                )
                return True
            await safe_reply(update.message, f"❌ {sm}")
            return True
    elif target is not None and target.state not in ("active", "idle"):
        # User aimed at a dead session (archived/lost/completed).
        # Silent fallback would route to active with no signal —
        # tell them so the routing surprise is visible. Falls
        # through to the active-session dispatch below.
        await safe_reply(
            update.message,
            f"⚠ \\[{target.name or target.id}\\] is {target.state} — "
            "routing to the active session instead.",
        )
    return False
