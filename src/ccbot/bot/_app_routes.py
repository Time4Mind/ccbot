"""Telegram Application builder and handler registration implementation.

The public entry point remains in :mod:`ccbot.bot.app`.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING, cast

from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..startup_queue import capture_startup_message

from ..config import config
from .callbacks import callback_handler
from .commands.auth import (
    login_command,
)
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
from .inbound import (
    command_intake_handler,
    document_intake_handler,
    photo_intake_handler,
    text_intake_handler,
    unsupported_intake_handler,
    voice_intake_handler,
)


if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    _error_handler = cast(Any, None)
    logger = cast(Any, None)
    post_init = cast(Any, None)
    post_shutdown = cast(Any, None)


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

    # Group -1 runs before commands and content handlers. It is a no-op unless
    # a new-session flow is open; while open it captures the update and stops
    # it from leaking to the previously-active session.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.StatusUpdate.ALL, capture_startup_message
        ),
        group=-1,
    )

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
    # /login stays out of setMyCommands: it is an emergency path surfaced by the
    # "authorization expired" notice (text + 🔐 button), not day-to-day UI.
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Forward any other /command to Claude Code.
    application.add_handler(MessageHandler(filters.COMMAND, command_intake_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_intake_handler)
    )
    application.add_handler(MessageHandler(filters.PHOTO, photo_intake_handler))
    application.add_handler(
        MessageHandler(filters.Document.ALL, document_intake_handler)
    )
    application.add_handler(MessageHandler(filters.VOICE, voice_intake_handler))
    # Catch-all: non-text content (stickers, video, etc.).
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_intake_handler,
        )
    )

    application.add_error_handler(_error_handler)

    return application
