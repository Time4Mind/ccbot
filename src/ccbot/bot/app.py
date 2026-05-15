"""Application lifecycle: bootstrap, ``setMyCommands``, recovery hooks,
status-polling startup, and handler registration.

Public entry point: ``create_bot()`` — called by ``ccbot.main``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import BotCommand
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
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
    status_command,
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


# Module-globals owned by the lifecycle hooks.
session_monitor: SessionMonitor | None = None
_status_poll_task: asyncio.Task[None] | None = None
_card_timer_task: asyncio.Task[None] | None = None
_quota_alerts_task: asyncio.Task[None] | None = None
_metrics_flush_task: asyncio.Task[None] | None = None


async def post_init(application: "Application[Any, Any, Any, Any, Any, Any]") -> None:
    """First task after Application is built. Publish menu, recover state, start monitors."""
    global \
        session_monitor, \
        _status_poll_task, \
        _card_timer_task, \
        _quota_alerts_task, \
        _metrics_flush_task

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


async def post_shutdown(
    application: "Application[Any, Any, Any, Any, Any, Any]",
) -> None:
    """Stop background tasks, flush queues, close HTTP clients."""
    global _status_poll_task, _card_timer_task, _quota_alerts_task, _metrics_flush_task

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

    if session_monitor:
        session_monitor.stop()
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
    application.add_handler(CommandHandler("status", status_command))
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

    return application
