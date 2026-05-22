"""``create_and_activate_session`` — tmux window creation flow.

Bridges the directory-browser / session-picker callbacks with the
``messages.text_handler`` "_pending_text" flow: creates a tmux window
(optionally ``claude --resume <id>``), registers a fresh ``Session``
record, makes it active, then forwards any held-over text the user typed
while the picker was up.

Lives in its own module so ``messages.py`` stays under the 600-LOC line.
"""

from __future__ import annotations

import logging

from telegram.ext import ContextTypes

from ..handlers.message_sender import safe_edit, safe_send
from ..handlers.notifications import (
    detach_paused_cards_at_message,
    paint_card_on_carrier,
)
from ..local_terminal import open_terminal_for_window
from ..session import session_manager
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


async def create_and_activate_session(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, register a Session, make it active, forward pending text."""
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    # Acknowledge the callback up-front so Telegram's 15-second
    # ``answer_callback_query`` deadline doesn't expire under slow
    # claude startup (Android Doze can stretch it to 30s+). All the
    # status feedback happens via ``safe_edit`` on the message itself.
    try:
        await query.answer()
    except Exception as e:
        logger.debug("Early query.answer failed: %s", e)

    # The carrier message is about to host this new session's "Created"
    # status — release any OLD card-state pause that was bound to the
    # same message_id. Otherwise the previously-active session's pause
    # never resumes and its events buffer forever, leaving the user
    # with a frozen card when they switch back via the switcher.
    if query.message is not None:
        detach_paused_cards_at_message(user.id, query.message.message_id)

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        selected_path, resume_session_id=resume_session_id
    )
    if not success:
        await safe_edit(query, f"❌ {message}")
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        return

    logger.info(
        "Window created: %s (id=%s) at %s (user=%d, resume=%s)",
        created_wname,
        created_wid,
        selected_path,
        user.id,
        resume_session_id,
    )
    # `claude --resume` records a new session_id in the hook, but messages
    # still write to the resumed JSONL. The card seeds from that existing
    # transcript, so for a resume we must resolve the canonical session_id
    # BEFORE painting — wait for the hook (or fall back to the known resume
    # id on timeout), then override window_state to track it.
    #
    # A fresh session has nothing to seed, so this pre-paint wait is
    # skipped entirely (see below): the empty card goes up the instant the
    # window exists instead of blocking on claude's 2-5s boot + the
    # SessionStart hook. The hook is confirmed (and the fresh session_id
    # bound) after the paint, before any pending text is forwarded.
    if resume_session_id:
        hook_ok = await session_manager.wait_for_session_map_entry(
            created_wid, timeout=15.0
        )
        ws = session_manager.get_window_state(created_wid)
        if not hook_ok:
            logger.warning(
                "Hook timed out for resume window %s, "
                "manually setting session_id=%s cwd=%s",
                created_wid,
                resume_session_id,
                selected_path,
            )
            ws.session_id = resume_session_id
            ws.cwd = str(selected_path)
            ws.window_name = created_wname
            session_manager.save_state()
        elif ws.session_id != resume_session_id:
            logger.info(
                "Resume override: window %s session_id %s -> %s",
                created_wid,
                ws.session_id,
                resume_session_id,
            )
            ws.session_id = resume_session_id
            session_manager.save_state()

    # Register Session record and make it active. Honor /new <name> if any.
    pending_name = (
        context.user_data.pop("_pending_session_name", "") if context.user_data else ""
    )
    sess = session_manager.create_session(
        name=pending_name or created_wname or "",
        window_id=created_wid,
        workdir=selected_path,
    )
    ws = session_manager.get_window_state(created_wid)
    if ws.session_id:
        session_manager.set_session_claude_id(sess.id, ws.session_id)
    session_manager.set_active_session(user.id, sess.id)

    if session_manager.get_user_settings(user.id).get("local_terminal") == "auto":
        await open_terminal_for_window(created_wid, user_id=user.id)

    # Transition the carrier from dir-browser to the new session's
    # empty live card in place. No separate "Created. Send messages
    # here." notice — that was a dead-end stub; the live card itself
    # is the destination and already conveys "this is the new session,
    # ready for input" via its header + standard footer.
    if query.message is not None:
        try:
            await paint_card_on_carrier(
                context.bot, user.id, sess, query.message.message_id
            )
        except Exception as e:
            logger.debug("paint new session card failed: %s", e)
            # Fallback: a minimal notice so the user isn't staring at
            # the stale dir-browser body when paint fails.
            await safe_edit(query, f"✅ {message}")

    # Fresh session: claude is still booting, so the hook hasn't written
    # the session_id yet. Confirm it now (card already on screen) and bind
    # it onto the Session record so the monitor + history follow the right
    # transcript and notifications reverse-map to this user — all before
    # any pending text is forwarded below.
    if not resume_session_id:
        await session_manager.wait_for_session_map_entry(created_wid, timeout=5.0)
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id and not sess.claude_session_id:
            session_manager.set_session_claude_id(sess.id, ws.session_id)

    # Forward any pending text held while the picker was up.
    pending_text = context.user_data.get("_pending_text") if context.user_data else None
    if pending_text:
        logger.debug(
            "Forwarding pending text to window %s (len=%d)",
            created_wname,
            len(pending_text),
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        send_ok, send_msg = await session_manager.send_to_window(
            created_wid, pending_text
        )
        if not send_ok:
            logger.warning("Failed to forward pending text: %s", send_msg)
            await safe_send(
                context.bot,
                user.id,
                f"❌ Failed to send pending message: {send_msg}",
            )
