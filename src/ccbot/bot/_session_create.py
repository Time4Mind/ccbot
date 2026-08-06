"""``create_and_activate_session`` — tmux window creation flow.

Bridges the directory-browser / session-picker callbacks with the
``messages.text_handler`` "_pending_text" flow: creates a tmux window
(optionally ``claude --resume <id>``), registers a fresh ``Session``
record, makes it active, then forwards any held-over text the user typed
while the picker was up.

Lives in its own module so ``messages.py`` stays under the 600-LOC line.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import ContextTypes

from ..handlers.message_sender import safe_edit
from ..handlers.notifications import (
    detach_paused_cards_at_message,
    paint_card_on_carrier,
)
from ..i18n import t
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

    if session_manager.agent_backend == "codex":
        from .commands.auth import ensure_codex_authenticated

        if not await ensure_codex_authenticated(context.bot, user.id):
            await safe_edit(
                query,
                t(user.id, "auth.codex.required"),
            )
            return

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        selected_path,
        resume_session_id=resume_session_id,
        owner_user_id=user.id,
        backend=session_manager.agent_backend,
    )
    if not success:
        await safe_edit(query, f"❌ {message}")
        return

    logger.info(
        "Window created: %s (id=%s) at %s (user=%d, resume=%s)",
        created_wname,
        created_wid,
        selected_path,
        user.id,
        resume_session_id,
    )
    # Publish the session immediately, while the agent process boots in the
    # pane. Every send is queued until the real TUI input prompt appears.
    # This covers fresh starts, normal resumes, and long resume compaction
    # with one ordering-preserving gate.
    session_manager.mark_window_starting(
        created_wid,
        backend=session_manager.agent_backend,
        resume=resume_session_id is not None,
        bot=context.bot,
        user_id=user.id,
    )

    # A resumed transcript id is already authoritative. Bind it before paint
    # instead of waiting up to 15 seconds for a lifecycle hook; the hook is
    # reconciled in the background below.
    if resume_session_id:
        ws = session_manager.get_window_state(created_wid)
        ws.session_id = resume_session_id
        ws.cwd = str(selected_path)
        ws.window_name = created_wname
        ws.backend = session_manager.agent_backend
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

    # Every inbound captured since the user pressed Start is now owned by
    # this window. The drain waits for proven TUI readiness and replays the
    # original Telegram updates in order.
    from ..startup_queue import bind_startup_queue

    bind_startup_queue(user.id, created_wid)

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

    async def _bind_lifecycle_in_background() -> None:
        """Attach the hook-written session id without delaying Telegram UI."""
        try:
            await session_manager.wait_for_session_map_entry(created_wid, timeout=15.0)
            live_ws = session_manager.get_window_state(created_wid)
            if resume_session_id:
                # Claude may expose a transient new id for ``--resume``;
                # messages still belong to the requested transcript.
                if live_ws.session_id != resume_session_id:
                    live_ws.session_id = resume_session_id
                    live_ws.cwd = str(selected_path)
                    live_ws.window_name = created_wname
                    live_ws.backend = session_manager.agent_backend
                    session_manager.save_state()
            elif live_ws.session_id and not sess.claude_session_id:
                session_manager.set_session_claude_id(sess.id, live_ws.session_id)
        except Exception as e:
            logger.warning(
                "Background lifecycle bind failed for window %s: %s",
                created_wid,
                e,
            )

    asyncio.create_task(
        _bind_lifecycle_in_background(), name=f"session-bind:{created_wid}"
    )

    # Desktop Terminal is a convenience side-effect, never part of the
    # session-start critical path.
    if session_manager.get_user_settings(user.id).get("local_terminal") == "auto":
        asyncio.create_task(
            open_terminal_for_window(created_wid, user_id=user.id),
            name=f"local-terminal:{created_wid}",
        )
