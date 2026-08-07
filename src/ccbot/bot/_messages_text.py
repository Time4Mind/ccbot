"""Text routing and background bash-output capture implementation.

Public imports remain in :mod:`ccbot.bot.messages`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot, Update
from telegram.ext import ContextTypes

from ..handlers.cleanup import clear_session_state
from ..handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    build_directory_browser,
)
from ..handlers.interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from ..handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_reply,
    send_with_fallback,
    try_rich_edit,
)
from ..handlers.notifications import (
    begin_repost_intent,
    card_is_below,
    end_repost_intent,
    get_card_state,
    is_active_for_user,
    lookup_session_for_message,
    refresh_panel,
    repost_card,
    resume_card_view,
)
from ..handlers.card_types import TurnPhase
from ..handlers.typing import fire_typing
from ..markdown_v2 import convert_markdown
from ..naming import maybe_auto_name
from ..session import session_manager
from ..terminal_parser import (
    extract_bash_output,
)
from ..tmux_manager import tmux_manager
from ._common import active_window, is_user_allowed
from .commands.auth import maybe_consume_code

from typing import Any, TYPE_CHECKING, cast

__all__ = [
    "_bash_capture_tasks",
    "cancel_bash_capture",
    "_capture_bash_output",
    "_route_reply_quote",
    "_resolve_active_window",
    "_maybe_start_bash_capture",
    "_dispatch_text_to_active",
    "text_handler",
]

if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    _await_prior_voice = cast(Any, None)
    _intercept_if_pending_ui = cast(Any, None)
    _send_with_delivery_proof = cast(Any, None)

logger = logging.getLogger(__name__)
# --- text + bash !cmd capture ---


# Active bash capture tasks: (user_id, window_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}


def cancel_bash_capture(user_id: int, window_id: str) -> None:
    """Cancel any running bash capture for this (user, window) pair."""
    key = (user_id, window_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
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
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
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
        _bash_capture_tasks.pop((user_id, window_id), None)


async def _route_reply_quote(update: Update, user_id: int, text: str) -> bool:
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
        tw = await tmux_manager.find_window_by_id(target.window_id)
        if tw:
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


async def _resolve_active_window(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    *,
    pinned_wid: str | None = None,
) -> str | None:
    """Resolve the active session's tmux window for the inbound text.

    Returns the window id when there is a live active session window.
    Returns None when ``text_handler`` must ``return`` instead — either
    because there is no active session (a directory browser is opened
    with the message queued) or because the active session's
    window is gone (it's marked lost, state cleared, and the user told).
    """
    assert update.message is not None
    wid = pinned_wid or active_window(user_id)
    if wid is None:
        # No active session — start a directory browser to create one.
        from ..startup_queue import begin_startup_queue, enqueue_startup_message

        begin_startup_queue(user_id)
        enqueue_startup_message(update, context)
        logger.info("No active session: showing directory browser (user=%d)", user_id)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = await build_directory_browser(
            start_path, user_id=user_id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return None

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info("Stale active session: window %s gone (user=%d)", display, user_id)
        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.mark_session_lost(sess.id)
        if active_window(user_id) == wid:
            await clear_session_state(user_id, wid, context.bot)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return None

    return wid


def _maybe_start_bash_capture(bot: Bot, user_id: int, wid: str, text: str) -> None:
    """Spawn the background ``!cmd`` pane-capture task for a ``!`` prefixed
    message. No-op for normal text. Records the task so a follow-up message
    can cancel it via :func:`cancel_bash_capture`."""
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]
        task = asyncio.create_task(_capture_bash_output(bot, user_id, wid, bash_cmd))
        _bash_capture_tasks[(user_id, wid)] = task


async def _dispatch_text_to_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    wid: str,
    text: str,
) -> bool:
    """Send the user's text to ``wid``'s pane and run the post-send
    bookkeeping under the repost-intent bracket.

    Card handling is gated on the target session still being the user's
    ACTIVE one. A voice message pins its window at receipt, so by the
    time whisper returns the user may well have switched elsewhere — the
    text still goes to the pinned pane (that is the entire point of
    pinning), but the session is a *background* one now, and background
    sessions never post their own chat messages. Doing otherwise dropped
    a bg session's card as the newest message in the chat and handed it
    the live switcher, which is what made a later switcher tap appear to
    edit "the previous message".

    Active path mirrors the original flow: resume the card view + arm
    repost-intent (so concurrent ``update_session_card`` events buffer
    rather than spawning a second card), send the keystrokes, fire the
    early typing indicator, touch + auto-name the session, spawn any
    ``!cmd`` capture, drive a pending interactive UI, and finally put the
    live card below the user's message. The try/finally always clears the
    repost-intent flag even on an early return.
    """
    assert update.message is not None
    import time as _time

    from .. import metrics
    from ..handlers import bg_status

    # If the user typed while looking at a Menu / sub-screen on this
    # session's card, drop the pause so incoming events render again.
    sess = session_manager.find_session_by_window(wid)
    owns_card = sess is not None and is_active_for_user(user_id, sess)
    if owns_card and sess is not None:
        await resume_card_view(context.bot, user_id, sess)
        # Lock spawning out from under us before sending keystrokes —
        # claude can emit the first event of its reply within
        # milliseconds of send_to_window returning, and
        # ``update_session_card`` would otherwise grab the card lock
        # first, see ``state.msg_id is None`` (from the previous turn's
        # ``finalize_task``) and spawn a fresh card just for that event.
        # ``repost_card`` would then spawn a SECOND card and try to
        # delete the first — succeeded delete loses claude's content,
        # failed delete leaves both visible (user-reported "2 от бота
        # после моего сообщения"). The buffer guarantees a single spawn.
        begin_repost_intent(user_id, sess.id)

    # Run the rest of the dispatch under a try/finally that always
    # clears the repost-intent flag — without this, an early return
    # below leaves the flag set forever and the live card stays silent
    # for that session until the bot restarts.
    intent_sess_id = sess.id if (owns_card and sess is not None) else None
    try:
        _t0 = _time.time()
        success, message = await _send_with_delivery_proof(wid, text, sess)
        metrics.observe("tg_to_claude_latency_ms", (_time.time() - _t0) * 1000.0)
        metrics.inc("tg_messages_in")
        if not success:
            metrics.inc("tg_send_failures")
            await safe_reply(update.message, f"❌ Delivery not confirmed: {message}")
            return False

        # Immediate typing-indicator so the user sees feedback within
        # ~500 ms of sending — claude can take 5-30 s before emitting
        # its first event (long tool prelude / thinking) and
        # ``status_polling`` won't fire typing until the pane enters
        # the busy-spinner state. Without this early fire the chat
        # looks frozen. fire_typing throttles to one call per ~4 s
        # per user — if text_handler already fired Typing a moment
        # ago, this is a silent no-op (the indicator is still on).
        if owns_card:
            await fire_typing(
                context.bot, user_id, "text_handler.post_send", window_id=wid
            )

        sess = session_manager.find_session_by_window(wid)
        # ``send_to_window`` and Codex's submit verification can take long
        # enough for the user to switch sessions.  The ``owns_card`` value
        # captured before those awaits is no longer authoritative: using it
        # below would let the old session resume/repost the carrier that the
        # switcher has already handed to the new active session.
        owns_card = sess is not None and is_active_for_user(user_id, sess)
        if sess is not None:
            get_card_state(user_id, sess).turn_phase = TurnPhase.RUNNING
            session_manager.touch_session(sess.id)
            # ``maybe_auto_name`` honours the user's ``haiku_naming``
            # setting and the directory-basename guard internally — we
            # only need to gate the call on a non-trivial seed (Haiku
            # can't summarise "hi" / "ok" into anything useful).
            if len(text) >= 20:
                asyncio.create_task(maybe_auto_name(sess.id, text, user_id))

        _maybe_start_bash_capture(context.bot, user_id, wid, text)

        if owns_card:
            interactive_window = get_interactive_window(user_id)
            if interactive_window and interactive_window == wid:
                await asyncio.sleep(0.2)
                await handle_interactive_ui(context.bot, user_id, wid)

        if sess is None:
            return True

        # Re-check immediately before the card mutation as well.  Auto-name,
        # interactive-UI handling, and other post-send work above may await.
        owns_card = is_active_for_user(user_id, sess)
        if not owns_card:
            # Background session (voice pinned here, user moved on).
            # Its only chat surface is a row in the active card's
            # bg-status panel — no card, no push, no switcher steal.
            if bg_status.update_status(user_id, sess.id, "working"):
                try:
                    await refresh_panel(context.bot, user_id)
                except Exception as e:
                    logger.debug("refresh_panel after bg dispatch failed: %s", e)
            return True

        # Put the live card below the user's message (the card_position
        # setting was ripped out — always-in-front is the single
        # canonical behaviour). Any events claude emitted between
        # send_to_window and here were buffered into state.events by
        # update_session_card (it saw the repost-intent flag and held
        # off rendering); they drain into the card on the next render.
        if card_is_below(user_id, sess.id, update.message.message_id):
            # The card is already in front of this message — the voice
            # flow reposted it at receipt. Repost again and the user
            # gets two cards' worth of churn for one voice; an in-place
            # edit is enough to drain the buffer and drop the pending row.
            try:
                await resume_card_view(context.bot, user_id, sess)
            except Exception as e:
                logger.debug("card repaint failed: %s", e)
        else:
            try:
                await repost_card(context.bot, user_id, sess)
            except Exception as e:
                logger.debug("repost_card failed: %s", e)
        return True
    finally:
        if intent_sess_id is not None:
            end_repost_intent(user_id, intent_sess_id)


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pinned_wid: str | None = None,
) -> bool:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Drop the message silently — no reply, no callback ack. The
        # allowlist is private; unauthorized senders should see the bot
        # as inert (no "not authorized" copy that signals "you found the
        # right bot, just not the right user").
        return False

    if not update.message or not update.message.text:
        return False

    text = update.message.text
    queued_wid = pinned_wid or active_window(user.id)
    if queued_wid is not None:
        if pinned_wid is None and not await _await_prior_voice(user.id, queued_wid):
            return False

    # A pending /login flow owns the next message: it's the OAuth code, not a
    # prompt. Must run before session routing — the code would otherwise be
    # typed into a pane (and echoed into that session's transcript).
    if await maybe_consume_code(update, context):
        return True

    # Ignore text while a picker UI is mid-flight.
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state in (
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_SESSION,
    ):
        await safe_reply(update.message, "Please use the picker above, or tap Cancel.")
        return False

    if await _route_reply_quote(update, user.id, text):
        return True

    wid = await _resolve_active_window(
        update, context, user.id, text, pinned_wid=pinned_wid
    )
    if wid is None:
        return False

    await fire_typing(context.bot, user.id, "text_handler", window_id=wid)

    # New message pushes pane content down — kill any in-flight bash capture.
    cancel_bash_capture(user.id, wid)

    # Pending AskUserQuestion / ExitPlanMode / Permission on the pane
    # would consume our keystrokes as menu navigation (digits select,
    # Enter submits). Surface the prompt to the user and bail before
    # send_to_window — the user must answer via the keyboard.
    if await _intercept_if_pending_ui(context.bot, user.id, wid, update.message):
        return False

    return await _dispatch_text_to_active(update, context, user.id, wid, text)
