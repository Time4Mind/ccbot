"""Application lifecycle: bootstrap, ``setMyCommands``, recovery hooks,
status-polling startup, and handler registration.

Public entry point: ``create_bot()`` — called by ``ccbot.main``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from telegram import BotCommand, Update
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import config
from ..handlers.quota_alerts import quota_alerts_loop
from ..handlers.notifications import card_timer_loop
from ..handlers.status_polling import status_poll_loop
from ..metrics import metrics_flush_loop
from ..session import session_manager
from ..session_monitor import NewMessage, SessionMonitor
from ..transcribe import close_client as close_transcribe_client
from ._common import CC_COMMANDS
from .callbacks import callback_handler
from .commands.info import (
    health_command,
    help_command,
    history_command,
    screenshot_command,
    usage_command,
)
from .commands.lifecycle import (
    archive_command,
    done_command,
    kill_command,
    menu_command,
    new_command,
    stop_command,
)
from .messages import (
    document_handler,
    forward_command_handler,
    photo_handler,
    text_handler,
    unsupported_content_handler,
    voice_handler,
)
from .session_events import handle_new_message

logger = logging.getLogger(__name__)


# Sustained ``Conflict`` ⇒ a second poller owns this token's getUpdates.
# Telegram's getUpdates is exclusive per token, so retrying can never
# recover — the only fix is for one instance to exit so the singleton
# flock + supervisor converge on exactly one live bot. A SINGLE transient
# Conflict (brief overlap during a normal restart) is tolerated. We act
# once EITHER threshold trips: ``CONFLICT_MAX_STREAK`` consecutive
# Conflicts, OR Conflicts persisting longer than ``CONFLICT_MAX_SECONDS``.
CONFLICT_MAX_STREAK = 3
CONFLICT_MAX_SECONDS = 15.0

# State for the sustained-Conflict detector, reset by any non-Conflict cycle.
_conflict_streak = 0
_conflict_first_seen: float | None = None

# Debounce identical consecutive transient-network log lines (A2c) — VPN
# drops produce the same line every 1-5 s; the bot self-recovers, so the
# repetition is pure noise.
_last_network_err_text: str | None = None


# Module-globals owned by the lifecycle hooks.
session_monitor: SessionMonitor | None = None
# Set in ``post_init`` so ``_error_handler`` can reach the Application even
# when ``update`` is not an Update (Conflict updates carry no chat).
_conflict_app: "Application[Any, Any, Any, Any, Any, Any] | None" = None
_status_poll_task: asyncio.Task[None] | None = None
_card_timer_task: asyncio.Task[None] | None = None
_quota_alerts_task: asyncio.Task[None] | None = None
_context_poll_task: asyncio.Task[None] | None = None
_metrics_flush_task: asyncio.Task[None] | None = None


async def post_init(application: "Application[Any, Any, Any, Any, Any, Any]") -> None:
    """First task after Application is built. Publish menu, recover state, start monitors."""
    global \
        session_monitor, \
        _status_poll_task, \
        _card_timer_task, \
        _quota_alerts_task, \
        _context_poll_task, \
        _metrics_flush_task, \
        _conflict_app

    # Reachable from ``_error_handler`` for the sustained-Conflict exit
    # path (Conflict updates carry no chat, so ``update`` is not an Update).
    _conflict_app = application

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

    # Context-poll loop disabled — sending /context to live panes
    # writes the modal's markdown output INTO the session's JSONL as
    # a user-turn, which then renders on the live card as a fake
    # ``[Request interrupted by user] ## Context Usage…`` block AND
    # eats real tokens from claude's own context window every cycle.
    # Context % is now computed from JSONL math (input + cache reads
    # vs. 1M default for Claude 4.x models). See ``usage.context_pct_for_session``.
    # _context_poll_task = asyncio.create_task(context_poll_loop(application.bot))
    # logger.info("Context poll task started")

    _metrics_flush_task = asyncio.create_task(metrics_flush_loop())
    logger.info("Metrics flush task started")

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
            if changed:
                try:
                    await refresh_panel(application.bot, user_id)
                except Exception as e:
                    logger.debug("refresh_panel after seed failed: %s", e)

    asyncio.create_task(_seed_bg_statuses())
    logger.info("Bg-status seed scheduled")


async def post_shutdown(
    application: "Application[Any, Any, Any, Any, Any, Any]",
) -> None:
    """Stop background tasks, flush queues, close HTTP clients."""
    global \
        _status_poll_task, \
        _card_timer_task, \
        _quota_alerts_task, \
        _context_poll_task, \
        _metrics_flush_task

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

    if _context_poll_task:
        _context_poll_task.cancel()
        try:
            await _context_poll_task
        except asyncio.CancelledError:
            pass
        _context_poll_task = None
        logger.info("Context poll stopped")

    if _metrics_flush_task:
        _metrics_flush_task.cancel()
        try:
            await _metrics_flush_task
        except asyncio.CancelledError:
            pass
        _metrics_flush_task = None
        logger.info("Metrics flush stopped")

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

    await close_transcribe_client()


def create_bot() -> "Application[Any, Any, Any, Any, Any, Any]":
    """Build the Application, wire all handlers, return it ready to run_polling."""
    builder = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if config.tg_proxy_url:
        # Route both long-poll and Bot API calls through TG_PROXY_URL.
        # Required when api.telegram.org is unreachable from the host.
        from telegram.request import HTTPXRequest

        builder = builder.request(
            HTTPXRequest(proxy=config.tg_proxy_url)
        ).get_updates_request(HTTPXRequest(proxy=config.tg_proxy_url))
        logger.info("TG proxy enabled: %s", config.tg_proxy_url)
    application = builder.build()

    # Visible menu commands.
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("archive", archive_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Forward any other /command to Claude Code.
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.).
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    application.add_error_handler(_error_handler)

    return application


def _terminate_for_sustained_conflict() -> None:
    """End this process so the supervisor restarts one clean instance.

    ``stop_running()`` is PTB's documented in-handler stop signal; it
    unwinds ``run_polling`` cleanly (post_shutdown fires). ``os._exit(1)``
    is the hard fallback so a half-stuck event loop can't leave the
    process alive-but-deaf — a non-zero code makes the supervisor treat
    it as a crash and respawn. Split out so tests can patch it.
    """
    app = _conflict_app
    if app is not None:
        try:
            app.stop_running()
        except Exception as e:
            logger.error("stop_running() failed during conflict exit: %s", e)
    os._exit(1)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB error handler — make exceptions visible AND actionable.

    Without this, PTB's default path is to log the raw traceback under
    ``telegram.ext.Application`` with no update / chat context attached
    — making it hard to tell which user / message triggered the bug.

    Behaviour:
    * ``Conflict`` means a second poller owns this token's exclusive
      getUpdates. A single transient one (restart overlap) is tolerated;
      a SUSTAINED storm (see ``CONFLICT_MAX_*``) is unrecoverable by
      retry, so we log CRITICAL and exit non-zero to let the singleton
      flock + supervisor converge on exactly one live bot.
    * Transient network errors (``NetworkError`` / ``TimedOut``) come
      from long-poll connection drops on flaky upstreams. The supervisor
      already loops on these and the AIORateLimiter retries Bot API
      calls. Log a one-liner at INFO; no stack trace noise.
    * ``RetryAfter`` is a Telegram-side rate-limit signal that
      AIORateLimiter handles already. INFO-level one-liner.
    * Everything else is a real bug. Log at ERROR with the full
      traceback AND whatever update / chat context we can extract.
    """
    global _conflict_streak, _conflict_first_seen, _last_network_err_text

    err = context.error
    if isinstance(err, Conflict):
        now = time.monotonic()
        if _conflict_first_seen is None:
            _conflict_first_seen = now
        _conflict_streak += 1
        elapsed = now - _conflict_first_seen
        logger.warning(
            "Telegram Conflict (streak=%d, %.1fs): %s",
            _conflict_streak,
            elapsed,
            err,
        )
        if _conflict_streak >= CONFLICT_MAX_STREAK or elapsed >= CONFLICT_MAX_SECONDS:
            logger.critical(
                "Sustained getUpdates Conflict (streak=%d, %.1fs) — a second "
                "poller owns this token. Exiting so the supervisor restarts a "
                "single clean instance.",
                _conflict_streak,
                elapsed,
            )
            _terminate_for_sustained_conflict()
        return
    # Any non-Conflict cycle clears the streak: a lone Conflict during a
    # normal restart overlap won't accumulate toward the threshold.
    _conflict_streak = 0
    _conflict_first_seen = None

    if isinstance(err, (NetworkError, TimedOut)):
        text = f"transient network error: {err}"
        if text != _last_network_err_text:
            logger.info("%s", text)
            _last_network_err_text = text
        return
    _last_network_err_text = None
    if isinstance(err, RetryAfter):
        logger.info("Telegram RetryAfter: %s", err)
        return
    user_id: int | None = None
    chat_id: int | None = None
    if isinstance(update, Update):
        if update.effective_user is not None:
            user_id = update.effective_user.id
        if update.effective_chat is not None:
            chat_id = update.effective_chat.id
    logger.exception(
        "Unhandled exception in handler (user=%s chat=%s): %s",
        user_id,
        chat_id,
        err,
        exc_info=err,
    )
