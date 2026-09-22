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
import time
from pathlib import Path
from typing import Any, Callable, cast

from telegram.ext import ContextTypes

from ..handlers.message_sender import safe_edit, safe_send
from ..handlers.notifications import (
    activate_card_on_carrier,
    paint_card_on_carrier,
    reset_card,
)
from ..i18n import t
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..transfer_runtime import get_node_runtime

logger = logging.getLogger(__name__)
_session_creation_tasks: dict[int, asyncio.Task[None]] = {}


def _log_phase(phase: str, user_id: int, node_id: str, started_at: float) -> None:
    logger.info(
        "session_create_phase",
        extra={
            "event": "session_create_phase",
            "phase": phase,
            "user_id": user_id,
            "node_id": node_id,
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        },
    )


async def create_and_activate_session(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    resume_session_id: str | None = None,
    node_id: str = "local",
) -> None:
    """Acknowledge Telegram and track session creation outside its handler."""
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    started_at = time.monotonic()
    try:
        await query.answer()
    except Exception as e:
        logger.debug("Early query.answer failed: %s", e)
    _log_phase("callback_acknowledged", user.id, node_id, started_at)

    existing = _session_creation_tasks.get(user.id)
    if existing is not None and not existing.done():
        logger.info("Session creation already running for user=%d", user.id)
        return

    selected_backend = (
        context.user_data.pop("_new_session_backend", None)
        if context.user_data is not None
        else None
    )
    flow_node_id = (
        context.user_data.pop("_new_session_node_id", None)
        if context.user_data is not None
        else None
    )
    backend = (
        selected_backend
        if selected_backend in ("claude", "codex")
        else session_manager.agent_backend
    )
    effective_backends = getattr(session_manager, "get_effective_backends", None)
    if flow_node_id is not None and flow_node_id != node_id:
        from ..startup_queue import cancel_startup_queue

        cancel_startup_queue(user.id)
        await safe_edit(query, "❌ Node selection expired - start again")
        return
    if callable(effective_backends) and backend not in cast(
        Callable[[int, str], tuple[str, ...]], effective_backends
    )(user.id, node_id):
        from ..startup_queue import cancel_startup_queue

        cancel_startup_queue(user.id)
        await safe_edit(query, "❌ Backend is no longer available on this node")
        return
    pending_name = (
        context.user_data.pop("_pending_session_name", "") if context.user_data else ""
    )
    previous_active = session_manager.get_active_session(user.id)

    task = asyncio.create_task(
        _create_and_activate_session(
            query,
            context,
            user,
            selected_path,
            resume_session_id=resume_session_id,
            node_id=node_id,
            backend=backend,
            pending_name=pending_name,
            previous_active=previous_active,
            started_at=started_at,
        ),
        name=f"session-create:{user.id}:{node_id}",
    )
    _session_creation_tasks[user.id] = task
    from ..startup_queue import track_startup_operation

    track_startup_operation(user.id, task)

    def _finish(done: asyncio.Task[None]) -> None:
        if _session_creation_tasks.get(user.id) is done:
            _session_creation_tasks.pop(user.id, None)
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background session creation failed for user=%d", user.id)

    task.add_done_callback(_finish)


async def wait_for_session_creation(user_id: int) -> None:
    """Wait for one tracked creation operation (test and shutdown seam)."""
    task = _session_creation_tasks.get(user_id)
    if task is not None:
        await asyncio.shield(task)


async def _create_and_activate_session(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    resume_session_id: str | None = None,
    node_id: str = "local",
    *,
    backend: str,
    pending_name: str,
    previous_active: Any,
    started_at: float,
) -> None:
    """Create a session and atomically replace the browser with its live card."""
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    # The Telegram surface is the acknowledgement for Start. Publish it before
    # any account probe, tmux/RPC startup, readiness polling, or lifecycle bind.
    # Until a real window is attached, the startup FIFO owns all user input.
    provisional_name = pending_name or Path(selected_path).name or "session"
    sess = session_manager.create_session(
        name=provisional_name,
        window_id="",
        workdir=selected_path,
        backend=backend,
        node_id=node_id,
    )
    if query.message is not None:
        await activate_card_on_carrier(
            user.id,
            previous_active.id if previous_active is not None else None,
            sess.id,
            query.message.message_id,
        )
        try:
            await paint_card_on_carrier(
                context.bot, user.id, sess, query.message.message_id
            )
        except Exception as e:
            logger.debug("paint new session card failed: %s", e)
            await safe_edit(query, "✅ Session is starting")
    else:
        session_manager.set_active_session(user.id, sess.id)
    from ..startup_queue import bind_startup_session

    startup_flow = bind_startup_session(user.id, sess.id)
    startup_window_published = asyncio.Event()

    async def _rollback_failed_local_codex_startup(
        failed_window_id: str, startup_error: BaseException
    ) -> None:
        await startup_window_published.wait()
        if session_manager.get_session(sess.id) is not sess:
            return
        if sess.window_id not in (failed_window_id, ""):
            return
        from ..startup_queue import (
            fail_startup_queue,
            report_failed_startup_entries,
        )

        queued = (
            fail_startup_queue(
                user.id,
                flow=startup_flow,
                window_id=failed_window_id,
            )
            if startup_flow is not None
            else []
        )
        session_manager.cancel_window_startup(failed_window_id)
        provider_session_id = sess.claude_session_id
        if provider_session_id:
            await session_manager.remove_provider_session_bindings(provider_session_id)
        else:
            session_manager.window_states.pop(failed_window_id, None)
            session_manager.window_display_names.pop(failed_window_id, None)
        session_manager.delete_session(sess.id)
        reset_card(user.id, sess.id)
        await report_failed_startup_entries(queued)
        await safe_send(
            context.bot,
            user.id,
            f"❌ Codex session did not start: {startup_error}",
        )
        logger.warning(
            "Local Codex startup rolled back user=%d session=%s window=%s: %s",
            user.id,
            sess.id,
            failed_window_id,
            startup_error,
        )

    _log_phase("card_published", user.id, node_id, started_at)

    def fail_startup(message: str) -> str:
        from ..startup_queue import fail_startup_queue

        logger.warning(
            "Session startup failed user=%d session=%s node=%s: %s",
            user.id,
            sess.id,
            node_id,
            message,
        )
        queued = (
            fail_startup_queue(user.id, flow=startup_flow)
            if startup_flow is not None
            else []
        )
        session_manager.delete_session(sess.id)
        reset_card(user.id, sess.id)
        lines = [message]
        for entry in queued:
            queued_message = entry.update.message
            if queued_message is None:
                label = "message"
            elif queued_message.text:
                label = queued_message.text
            elif queued_message.voice:
                label = "voice message"
            elif queued_message.photo:
                label = "photo"
            elif queued_message.document:
                label = "document"
            else:
                label = "message"
            lines.append(t(user.id, "startup.prompt_not_sent", prompt=label))
        return "\n".join(lines)

    if node_id == "local" and backend == "codex":
        from .commands.auth import ensure_codex_authenticated

        authenticated = await ensure_codex_authenticated(
            context.bot, user.id, backend=backend
        )
        _log_phase("auth_result", user.id, node_id, started_at)
        if not authenticated:
            failure = fail_startup(t(user.id, "auth.codex.required"))
            await safe_edit(
                query,
                failure,
            )
            return

    agent_session_id = ""
    if node_id == "local":
        if backend == "codex":
            create_result = await tmux_manager.create_window(
                selected_path,
                resume_session_id=resume_session_id,
                backend=backend,
                wait_for_codex_ready=False,
                on_startup_failure=_rollback_failed_local_codex_startup,
            )
        else:
            create_result = await tmux_manager.create_window(
                selected_path,
                resume_session_id=resume_session_id,
                backend=backend,
                wait_for_codex_ready=False,
            )
        success, message, created_wname, created_wid = create_result
        if not success:
            startup_window_published.set()
            failure = fail_startup(message)
            await safe_edit(query, f"❌ {failure}")
            return
    else:
        runtime = get_node_runtime(node_id)
        if runtime is None:
            failure = fail_startup(f"Node is not connected: {node_id}")
            await safe_edit(query, f"❌ {failure}")
            return
        if resume_session_id:
            failure = fail_startup("Remote resume is not supported")
            await safe_edit(
                query,
                f"❌ {failure}",
            )
            return
        try:
            result = await runtime.create_session(
                node_id,
                selected_path,
                backend,
                pending_name or "session",
            )
        except Exception as exc:
            logger.exception("Remote session creation failed on node %s", node_id)
            failure = fail_startup(str(exc))
            await safe_edit(query, f"❌ {failure}")
            return
        raw_window_id = str(result.get("target_window_id", ""))
        agent_session_id = str(result.get("target_agent_session_id", ""))
        if not raw_window_id or not agent_session_id:
            failure = fail_startup("Worker did not return a ready session")
            await safe_edit(query, f"❌ {failure}")
            return
        created_wid = f"{node_id}::{raw_window_id}"
        created_wname = pending_name or raw_window_id
        message = "Remote session created"

    sess.window_id = created_wid
    session_manager.save_state()
    _log_phase("process_started", user.id, node_id, started_at)

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
    if node_id == "local":
        session_manager.mark_window_starting(
            created_wid,
            backend=backend,
            resume=resume_session_id is not None,
            bot=context.bot,
            user_id=user.id,
        )

    # A resumed transcript id is already authoritative. Bind it before paint
    # instead of waiting up to 15 seconds for a lifecycle hook; the hook is
    # reconciled in the background below.
    if node_id == "local" and resume_session_id:
        ws = session_manager.get_window_state(created_wid)
        ws.session_id = resume_session_id
        ws.cwd = str(selected_path)
        ws.window_name = created_wname
        ws.backend = backend
        session_manager.save_state()

    if node_id == "local":
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id:
            session_manager.set_session_claude_id(sess.id, ws.session_id)
    else:
        session_manager.set_session_worker_id(sess.id, agent_session_id)
    # Every inbound captured since the user pressed Start is now owned by
    # this window. Binding happens after the initial paint so a queued user's
    # visual receipt can move the finished card below that Telegram message
    # without racing the directory-browser handoff. Delivery itself remains
    # gated on proven TUI readiness.
    from ..startup_queue import bind_startup_queue

    bind_startup_queue(user.id, created_wid, flow=startup_flow)
    startup_window_published.set()

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
                    live_ws.backend = backend
                    session_manager.save_state()
            elif live_ws.session_id and not sess.claude_session_id:
                session_manager.set_session_claude_id(sess.id, live_ws.session_id)
            _log_phase("lifecycle_bound", user.id, node_id, started_at)
        except Exception as e:
            logger.warning(
                "Background lifecycle bind failed for window %s: %s",
                created_wid,
                e,
            )

    if node_id == "local":
        asyncio.create_task(
            _bind_lifecycle_in_background(), name=f"session-bind:{created_wid}"
        )
