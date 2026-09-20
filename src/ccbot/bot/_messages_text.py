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
    BROWSE_NODE_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
    STATE_PREPROCESSING_INSTRUCTION,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    build_directory_browser,
)
from ..handlers.interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from ..handlers.message_sender import (
    safe_reply,
)
from ..handlers.notifications import (
    begin_repost_intent,
    card_is_below,
    end_repost_intent,
    get_card_state,
    is_active_for_user,
    refresh_panel,
    repost_card,
)
from ..handlers.card_types import TurnPhase
from ..handlers.typing import fire_typing
from ..naming import maybe_auto_name
from ..i18n import t
from ..remote_prompt_queue import remote_prompt_queue
from ..session import session_manager
from ..transfer_runtime import get_node_runtime
from ..tmux_manager import tmux_manager
from ._common import active_window, is_user_allowed
from ._messages_capture import (
    bash_capture_tasks as _bash_capture_tasks,
    cancel_bash_capture,
    capture_bash_output as _capture_bash_output,
    route_reply_quote as _route_reply_quote,
)
from ._messages_preprocessing import prepare_request_for_dispatch
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
        from .callbacks.dir_browser import initialize_directory_browser

        msg_text, keyboard, _subdirs = await initialize_directory_browser(
            context, user_id
        )
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return None

    sess = session_manager.find_session_by_window(wid)
    if sess is not None and getattr(sess, "node_id", "local") != "local":
        if get_node_runtime(getattr(sess, "node_id", "local")) is None:
            await safe_reply(update.message, "❌ Remote node is not connected.")
            return None
        return wid

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
    session = session_manager.find_session_by_window(wid)
    if session is not None and getattr(session, "node_id", "local") != "local":
        return
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
    *,
    input_kind: str = "text",
    _from_remote_queue: bool = False,
    _prepared_dispatch: Any = None,
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

    initial_session = session_manager.find_session_by_window(wid)
    initial_node_id = (
        getattr(initial_session, "node_id", "local")
        if initial_session is not None
        else "local"
    )
    if (
        not _from_remote_queue
        and initial_session is not None
        and isinstance(initial_node_id, str)
        and initial_node_id != "local"
    ):
        node_id = initial_node_id
        node = session_manager.get_node(node_id)
        if remote_prompt_queue.has_pending(initial_session.id) or (
            node is None or not node.is_available()
        ):
            queued_prepared_dispatch: Any = None

            async def deliver() -> bool:
                nonlocal queued_prepared_dispatch
                if queued_prepared_dispatch is None:
                    queued_prepared_dispatch = await prepare_request_for_dispatch(
                        update,
                        context,
                        user_id,
                        wid,
                        text,
                        input_kind=input_kind,
                        persist_recovery=False,
                    )
                return await _dispatch_text_to_active(
                    update,
                    context,
                    user_id,
                    wid,
                    text,
                    input_kind=input_kind,
                    _from_remote_queue=True,
                    _prepared_dispatch=queued_prepared_dispatch,
                )

            return await remote_prompt_queue.admit(
                original_message=update.message,
                session_id=initial_session.id,
                node_id=node_id,
                node_name=node.display_name if node is not None else node_id,
                deliver=deliver,
            )

    prepared_dispatch = _prepared_dispatch
    if prepared_dispatch is None:
        prepared_dispatch = await prepare_request_for_dispatch(
            update,
            context,
            user_id,
            wid,
            text,
            input_kind=input_kind,
        )
    text = prepared_dispatch.text

    # Navigation is authoritative. A session keeps accepting its pinned
    # prompt while Menu / Settings / Archive is open, but must remain a
    # background surface until the user explicitly returns to its card.
    sess = session_manager.find_session_by_window(wid)
    card_state = None
    owns_card = sess is not None and is_active_for_user(user_id, sess)
    if owns_card and sess is not None:
        card_state = get_card_state(user_id, sess)
    if (
        owns_card
        and sess is not None
        and card_state is not None
        and not card_state.in_menu_view
    ):
        # Buffer events until the visible card is reposted below the prompt.
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

        # Delivery was confirmed to the originally pinned pane. Only now is
        # it safe to remove the restart record and persist the transcript
        # marker; a crash before this point leaves enough data for recovery.
        prepared_dispatch.confirm_delivery()

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
        # Re-check ownership after awaits so an old session cannot steal the
        # carrier back after the user switches.
        owns_card = sess is not None and is_active_for_user(user_id, sess)
        status_changed = False
        if sess is not None:
            card_state = get_card_state(user_id, sess)
            card_state.turn_phase = TurnPhase.RUNNING
            status_changed = bg_status.update_status(
                user_id, sess.id, "working", force=True
            )
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
            # Background delivery stays silent in chat.
            if status_changed:
                try:
                    await refresh_panel(context.bot, user_id)
                except Exception as e:
                    logger.debug("refresh_panel after bg dispatch failed: %s", e)
            return True

        # The prompt may take seconds to reach Codex. During that wait the
        # user can tap New/Menu on the receipt card. That newer navigation is
        # authoritative: do not let this older inbound handler clear
        # ``in_menu_view`` and repaint the session over the directory browser.
        if card_state is not None and card_state.in_menu_view:
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
                await refresh_panel(
                    context.bot,
                    user_id,
                    immediate=True,
                    refresh_pane=False,
                )
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
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state == STATE_PREPROCESSING_INSTRUCTION:
        instruction = text.strip()
        if (
            not instruction
            or len(instruction.encode("utf-8")) > 16 * 1024
            or "\x00" in instruction
        ):
            await safe_reply(
                update.message, t(user.id, "preprocessing.instruction.invalid")
            )
            return True
        session_manager.update_user_setting(
            user.id, "preprocessing_instruction", instruction
        )
        if context.user_data is not None:
            context.user_data.pop(STATE_KEY, None)
        from ..handlers.menu import build_footer_keyboard, render_settings_group_text

        await safe_reply(
            update.message,
            render_settings_group_text(user.id, "settings_cat_preprocessing"),
            reply_markup=build_footer_keyboard(
                user.id, screen="settings_cat_preprocessing"
            ),
        )
        return True
    if state == STATE_NAMING_DIRECTORY:
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY) if context.user_data else None
        )
        name = text.strip()
        if (
            not current_path
            or not name
            or name in (".", "..")
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            await safe_reply(
                update.message, "Некорректное имя папки. Введите одно имя без слешей."
            )
            return True
        node_id = (
            context.user_data.get(BROWSE_NODE_KEY, "local")
            if context.user_data
            else "local"
        )
        if node_id != "local":
            runtime = get_node_runtime(node_id)
            if runtime is None:
                await safe_reply(update.message, "❌ Remote node is not connected.")
                return True
            try:
                result = await runtime.create_directory(node_id, current_path, name)
            except Exception as exc:
                logger.exception("Remote directory creation failed on node %s", node_id)
                await safe_reply(update.message, f"❌ {exc}")
                return True
            target_path = str(result.get("path", ""))
            subdirs = [str(value) for value in result.get("directories", [])]
            if not target_path:
                await safe_reply(update.message, t(user.id, "dir.create.failed"))
                return True
            msg_text, keyboard, _ = await build_directory_browser(
                target_path,
                user_id=user.id,
                remote_subdirs=subdirs,
                remote=True,
            )
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
                context.user_data[BROWSE_PATH_KEY] = target_path
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            notice_key = (
                "dir.create.exists"
                if result.get("existed", False)
                else "dir.create.created"
            )
            await safe_reply(
                update.message,
                f"{t(user.id, notice_key)}\n\n{msg_text}",
                reply_markup=keyboard,
            )
            return True
        target = (Path(current_path) / name).resolve()
        if target.parent != Path(current_path).resolve():
            await safe_reply(update.message, "Некорректное имя папки.")
            return True
        existed = False
        try:
            target.mkdir()
        except FileExistsError:
            if not target.is_dir():
                await safe_reply(update.message, t(user.id, "dir.create.failed"))
                return True
            existed = True
        except OSError:
            await safe_reply(update.message, t(user.id, "dir.create.failed"))
            return True
        msg_text, keyboard, subdirs = await build_directory_browser(
            str(target), user_id=user.id
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = str(target)
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        notice_key = "dir.create.exists" if existed else "dir.create.created"
        await safe_reply(
            update.message,
            f"{t(user.id, notice_key)}\n\n{msg_text}",
            reply_markup=keyboard,
        )
        return True

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
    if state in (
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_SESSION,
    ):
        await safe_reply(update.message, "Please use the picker above, or tap Cancel.")
        return False

    if pinned_wid is None and await _route_reply_quote(update, user.id, text):
        return True

    wid = await _resolve_active_window(
        update, context, user.id, text, pinned_wid=pinned_wid
    )
    if wid is None:
        return False

    await fire_typing(context.bot, user.id, "text_handler", window_id=wid)

    # New message pushes pane content down — kill any in-flight bash capture.
    cancel_bash_capture(user.id, wid)

    # A pending interactive prompt would consume our keystrokes as navigation.
    # Pinned FIFO requests wait; only the non-queued fallback asks for a resend.
    if await _intercept_if_pending_ui(
        context.bot,
        user.id,
        wid,
        update.message,
        wait_until_clear=pinned_wid is not None,
    ):
        return False

    return await _dispatch_text_to_active(update, context, user.id, wid, text)
