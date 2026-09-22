"""Telegram Application builder and handler registration implementation.

The public entry point remains in :mod:`ccbot.bot.app`.
"""

from __future__ import annotations

import socket
from typing import Any, TYPE_CHECKING, cast

import httpx
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..startup_queue import capture_startup_message
from ..telegram_rate_limit import PersistentEndpointRateLimiter
from ..telegram_polling_health import PollingHeartbeatRequest

from ..config import config
from .callbacks import callback_handler
from .activity import record_user_message_activity
from .commands.auth import (
    login_command,
)
from .commands.info import (
    health_command,
    help_command,
    usage_command,
)
from .commands.lifecycle import (
    archive_command,
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
    polling_health = cast(Any, None)
    _record_poll_success = cast(Any, None)


def _socket_options() -> list[tuple[int, int, int]]:
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPINTVL", 30),
        ("TCP_KEEPCNT", 3),
    ):
        if setting := getattr(socket, name, None):
            options.append((socket.IPPROTO_TCP, setting, value))
    return options


def _request_pair() -> tuple[Any, PollingHeartbeatRequest]:
    from telegram.request import HTTPXRequest

    def request(*, pool_size: int, read_timeout: float) -> Any:
        transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(
                max_connections=pool_size,
                max_keepalive_connections=pool_size,
            ),
            proxy=config.tg_proxy_url or None,
            socket_options=_socket_options(),
        )
        return HTTPXRequest(
            read_timeout=read_timeout,
            connect_timeout=10.0,
            write_timeout=10.0,
            pool_timeout=10.0,
            httpx_kwargs={"transport": transport},
        )

    general = request(pool_size=16, read_timeout=20.0)
    polling = request(pool_size=4, read_timeout=15.0)
    return general, PollingHeartbeatRequest(
        polling, polling_health, on_success=_record_poll_success
    )


def create_bot() -> "Application[Any, Any, Any, Any, Any, Any]":
    """Build the Application, wire all handlers, return it ready to run_polling."""
    general_request, polling_request = _request_pair()
    builder = (
        Application.builder()
        .token(config.telegram_bot_token)
        .request(general_request)
        .get_updates_request(polling_request)
        .rate_limiter(
            PersistentEndpointRateLimiter(
                token=config.telegram_bot_token,
                cooldown_path=config.config_dir / "telegram-rate-limits.json",
            )
        )
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if config.tg_proxy_url:
        logger.info("TG proxy enabled")
    application = builder.build()

    # Activity is observed in its own earlier group so messages captured by
    # the new-session flow and visible slash commands count as user actions
    # too. This handler never stops propagation.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.StatusUpdate.ALL,
            record_user_message_activity,
        ),
        group=-2,
    )

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
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("kill", kill_command))
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
