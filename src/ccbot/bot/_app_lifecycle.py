"""Application startup and shutdown implementation.

Public entry points remain in :mod:`ccbot.bot.app`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, TYPE_CHECKING, cast

from telegram import BotCommand
from telegram.ext import (
    Application,
)

from ..inbound_queue import shutdown_inbound_queues

from ..config import config
from ..handlers.quota_alerts import quota_alerts_loop
from ..handlers.notifications import card_timer_loop, shutdown_card_surface_tasks
from ..handlers.status_polling import status_poll_loop
from ..metrics import metrics_flush_loop
from ..session import session_manager
from ..session_monitor import NewMessage, SessionMonitor
from ._common import CC_COMMANDS
from .commands.auth import (
    ensure_codex_auth_on_start,
    shutdown_auth_flows,
)
from .session_events import handle_new_message

# Module-globals owned by the lifecycle hooks.

if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    LIVENESS_MAX_STALE_SECONDS = cast(Any, None)
    _heartbeat_loop = cast(Any, None)
    _liveness_watchdog_loop = cast(Any, None)
    logger = cast(Any, None)

session_monitor: SessionMonitor | None = None
# Set in ``post_init`` so ``_error_handler`` can reach the Application even
# when ``update`` is not an Update (Conflict updates carry no chat).
_conflict_app: "Application[Any, Any, Any, Any, Any, Any] | None" = None
_status_poll_task: asyncio.Task[None] | None = None
_card_timer_task: asyncio.Task[None] | None = None
_quota_alerts_task: asyncio.Task[None] | None = None
_metrics_flush_task: asyncio.Task[None] | None = None
_heartbeat_task: asyncio.Task[None] | None = None
_auth_preflight_task: asyncio.Task[None] | None = None
_usage_prewarm_task: asyncio.Task[None] | None = None
_send_file_relay_task: asyncio.Task[None] | None = None


async def post_init(application: "Application[Any, Any, Any, Any, Any, Any]") -> None:
    """First task after Application is built. Publish menu, recover state, start monitors."""
    global \
        session_monitor, \
        _status_poll_task, \
        _card_timer_task, \
        _quota_alerts_task, \
        _metrics_flush_task, \
        _heartbeat_task, \
        _auth_preflight_task, \
        _usage_prewarm_task, \
        _send_file_relay_task, \
        _last_heartbeat, \
        _conflict_app

    # Reachable from ``_error_handler`` for the sustained-Conflict exit
    # path (Conflict updates carry no chat, so ``update`` is not an Update).
    _conflict_app = application

    # Agent sessions may have neither network nor cross-process socket access.
    # Consume filesystem-relay requests and perform Telegram delivery here.
    from ..send_file import send_file_relay_loop

    _send_file_relay_task = asyncio.create_task(send_file_relay_loop(application.bot))

    # Warm the directory browser's recursive index off the startup path. The
    # picker itself always paints from cache/shallow metadata and never waits
    # for this scan.
    from ..handlers.directory_browser import prewarm_directory_recency

    prewarm_directory_recency()
    logger.info("Directory-recency cache pre-warm scheduled")

    # Cache bot username so ``tmux_manager.create_window`` can surface it
    # to Claude via ``CCBOT_BOT_USERNAME``. ``application.bot.username``
    # triggers a ``getMe`` if not already populated; with ``initialize()``
    # already done by run_polling this is a cached property.
    try:
        config.bot_username = application.bot.username or ""
    except Exception as e:
        logger.debug("Could not resolve bot.username: %s", e)

    await application.bot.delete_my_commands()

    # Trimmed /-menu surface. New/Status/Shot/Settings/Archive all live
    # behind the inline ≡ Menu; Stop/Kill/Clear in the live-card footer.
    # ``/history`` is published — it's the canonical entry to the FULL
    # JSONL transcript view (deep history); the live card itself only
    # seeds the last CARD_SEED_TURNS end-of-turn boundaries.
    # Hidden commands still work when typed.
    bot_commands = [
        BotCommand("menu", "Open menu"),
        BotCommand("help", "Quick guide / inline doc"),
        BotCommand("history", "Full transcript of the active session"),
        BotCommand("done", "Mark a session as done"),
    ]
    for cmd_name in ("model", "effort", "compact", "memory"):
        if cmd_name in CC_COMMANDS:
            bot_commands.append(BotCommand(cmd_name, CC_COMMANDS[cmd_name]))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows.
    await session_manager.resolve_stale_ids()
    # DM mode: cross-check Session records against live tmux. Sessions whose
    # window vanished get state=lost and surface in the switcher with a
    # Restore button.
    await session_manager.reconcile_sessions_with_tmux()

    # A fresh Codex host should be operable from Telegram alone. Read auth
    # state after the bot is online and automatically start the official
    # device-code flow when no account is present.
    _auth_preflight_task = asyncio.create_task(
        ensure_codex_auth_on_start(application.bot)
    )
    logger.info("Agent auth preflight scheduled")

    async def _prewarm_live_usage() -> None:
        """Populate Status cache off the user interaction path after auth."""
        try:
            if _auth_preflight_task is not None:
                await _auth_preflight_task
            from ._usage_window import fetch_live_usage

            info = await fetch_live_usage()
            logger.info("Live usage cache pre-warmed ok=%s", info is not None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Live usage cache pre-warm failed: %s", e)

    _usage_prewarm_task = asyncio.create_task(_prewarm_live_usage())
    logger.info("Live usage cache pre-warm scheduled")

    # Pre-fill global rate limiter bucket on restart. AsyncLimiter starts at
    # _level=0 (full burst capacity), but Telegram's server-side counter
    # persists across bot restarts. Force the bucket to start "full" so
    # capacity drains in naturally (~1s).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    _card_timer_task = asyncio.create_task(card_timer_loop(application.bot))
    logger.info("Card timer task started")

    _quota_alerts_task = asyncio.create_task(quota_alerts_loop(application.bot))
    logger.info("Quota alerts task started")

    # Per-session context % is computed from JSONL math
    # (usage.context_pct_for_session) — NOT by polling /context into panes.
    # Polling wrote the modal's markdown into each session's JSONL as a fake
    # user-turn (polluting the live card + burning tokens), so that path was
    # removed. See doc/dm-multisession-spec.md §4.6.

    _metrics_flush_task = asyncio.create_task(metrics_flush_loop())
    logger.info("Metrics flush task started")

    _last_heartbeat = time.monotonic()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    threading.Thread(
        target=_liveness_watchdog_loop, daemon=True, name="ccbot-liveness-watchdog"
    ).start()
    logger.info(
        "Liveness watchdog started (stale>%.0fs triggers exit)",
        LIVENESS_MAX_STALE_SECONDS,
    )

    # Pre-warm the history-page cache for every active/idle session so
    # the user's first switcher tap after a restart doesn't pay the
    # ~1 s parse cost of walking a multi-thousand-message JSONL. Runs
    # off the boot path so it can't delay the bot coming online.
    async def _prewarm_history_caches() -> None:
        from ..handlers.history import prewarm_pages_cache

        for sess in list(session_manager.sessions.values()):
            if sess.state not in ("active", "idle") or not sess.window_id:
                continue
            try:
                await prewarm_pages_cache(sess.window_id)
            except Exception as e:
                logger.debug("prewarm failed for %s: %s", sess.window_id, e)

    asyncio.create_task(_prewarm_history_caches())
    logger.info("History cache pre-warm scheduled")

    # Seed bg_status for sessions that are still "working" so a
    # restart-spanned in-progress session lands in the panel as soon
    # as the bot comes up. ``finished`` sessions are NOT seeded —
    # they're already-completed turns; if the user noticed them
    # before the restart they don't need a repeat notification, and
    # if they didn't they can switch into the session to see the
    # answer. The fresh-end-of-turn notification path
    # (session_events) still fires for sessions that actually
    # finish AFTER the bot starts.
    async def _seed_bg_statuses() -> None:
        from ..handlers import bg_status
        from ..handlers.notifications import refresh_panel
        from ..usage import context_pct_for_session

        for user_id in config.allowed_users:
            active = session_manager.get_active_session(user_id)
            active_id = active.id if active is not None else None
            changed = False
            for sess in list(session_manager.sessions.values()):
                if sess.state not in ("active", "idle"):
                    continue
                if sess.id == active_id:
                    continue
                try:
                    inferred = await bg_status.infer_status_from_jsonl(sess)
                except Exception as e:
                    logger.debug("infer bg status failed for %s: %s", sess.id, e)
                    continue
                if inferred != "working":
                    continue
                if bg_status.update_status(user_id, sess.id, "working"):
                    changed = True
                try:
                    pct = await context_pct_for_session(sess)
                except Exception as e:
                    logger.debug("infer bg context failed for %s: %s", sess.id, e)
                    pct = None
                if pct is not None:
                    bg_status.set_context_pct(user_id, sess.id, pct)
                    changed = True
            if changed:
                try:
                    await refresh_panel(application.bot, user_id)
                except Exception as e:
                    logger.debug("refresh_panel after seed failed: %s", e)

    asyncio.create_task(_seed_bg_statuses())
    logger.info("Bg-status seed scheduled")

    # Repaint each user's persisted live card in place. ``_cards`` is
    # in-memory only, so without this a restart orphans the card message
    # in chat and a fresh one appears on the next event. ``restore_card``
    # rebuilds the CardState, seeds the recent transcript, and edits the
    # original message so the live card resumes on the same message.
    async def _restore_active_cards() -> None:
        from ..handlers.notifications import restore_card

        for user_id in config.allowed_users:
            card_msg_id = session_manager.get_card_msg(user_id)
            if not card_msg_id:
                continue
            active = session_manager.get_active_session(user_id)
            if active is None:
                continue
            try:
                ok = await restore_card(application.bot, user_id, active, card_msg_id)
                logger.info(
                    "Restored live card user=%d session=%s msg=%d ok=%s",
                    user_id,
                    active.id,
                    card_msg_id,
                    ok,
                )
            except Exception as e:
                logger.debug("restore_card failed for user %d: %s", user_id, e)

    asyncio.create_task(_restore_active_cards())
    logger.info("Active-card restore scheduled")


async def post_shutdown(
    application: "Application[Any, Any, Any, Any, Any, Any]",
) -> None:
    """Stop background tasks, flush queues, close HTTP clients."""
    global \
        _status_poll_task, \
        _card_timer_task, \
        _quota_alerts_task, \
        _metrics_flush_task, \
        _heartbeat_task, \
        _auth_preflight_task, \
        _usage_prewarm_task, \
        _send_file_relay_task

    if _usage_prewarm_task:
        if not _usage_prewarm_task.done():
            _usage_prewarm_task.cancel()
        await asyncio.gather(_usage_prewarm_task, return_exceptions=True)
        _usage_prewarm_task = None

    if _auth_preflight_task:
        if not _auth_preflight_task.done():
            _auth_preflight_task.cancel()
        await asyncio.gather(_auth_preflight_task, return_exceptions=True)
        _auth_preflight_task = None
    await shutdown_auth_flows()
    await shutdown_inbound_queues()
    await shutdown_card_surface_tasks()

    if _send_file_relay_task:
        _send_file_relay_task.cancel()
        await asyncio.gather(_send_file_relay_task, return_exceptions=True)
        _send_file_relay_task = None
        logger.info("send-file filesystem relay stopped")

    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    if _card_timer_task:
        _card_timer_task.cancel()
        try:
            await _card_timer_task
        except asyncio.CancelledError:
            pass
        _card_timer_task = None
        logger.info("Card timer stopped")

    if _quota_alerts_task:
        _quota_alerts_task.cancel()
        try:
            await _quota_alerts_task
        except asyncio.CancelledError:
            pass
        _quota_alerts_task = None
        logger.info("Quota alerts stopped")

    if _metrics_flush_task:
        _metrics_flush_task.cancel()
        try:
            await _metrics_flush_task
        except asyncio.CancelledError:
            pass
        _metrics_flush_task = None
        logger.info("Metrics flush stopped")

    if _heartbeat_task:
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        _heartbeat_task = None
        logger.info("Liveness heartbeat stopped")

    # Drain anything spawned by the handlers BEFORE we stop the
    # session monitor — both helpers do real I/O (history JSONL reads,
    # editMessageText calls) that we'd rather see finish or get
    # cancelled cleanly instead of being abandoned with the loop.
    from ..handlers.history import cancel_pending_prewarm
    from ..handlers.notifications import cancel_pending_card_edits

    await cancel_pending_card_edits()
    await cancel_pending_prewarm()

    if session_monitor:
        await session_monitor.stop()
        logger.info("Session monitor stopped")
