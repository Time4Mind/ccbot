# ruff: noqa: F401
# pyright: reportUnusedImport=false, reportUnusedFunction=false
"""Application facade plus polling-failure and liveness safeguards.

Lifecycle startup/shutdown and handler registration live in private sibling
modules. Their public entry points and mutable compatibility surface stay here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from types import ModuleType as _ModuleType
from typing import Any, TYPE_CHECKING, cast

from . import _app_lifecycle as _lifecycle_impl
from . import _app_routes as _routes_impl

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

from ..startup_queue import capture_startup_message
from ..inbound_queue import shutdown_inbound_queues

from ..config import config
from ..handlers.quota_alerts import quota_alerts_loop
from ..handlers.notifications import card_timer_loop, shutdown_card_surface_tasks
from ..handlers.status_polling import status_poll_loop
from ..metrics import metrics_flush_loop
from ..session import session_manager
from ..session_monitor import NewMessage, SessionMonitor
from ._common import CC_COMMANDS
from .callbacks import callback_handler
from .commands.auth import (
    ensure_codex_auth_on_start,
    login_command,
    shutdown_auth_flows,
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
from .session_events import handle_new_message


if TYPE_CHECKING:
    # Runtime-injected by the compatibility facade before each call.
    _conflict_app = cast(Any, None)

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

# Sustained network outage ⇒ the silent twin of the Conflict storm. The
# long-poll getUpdates keeps raising NetworkError/TimedOut and the bot can
# stay alive-but-deaf, NOT recovering even after the upstream returns (a
# wedged half-open poll socket). So once network errors persist CONTIGUOUSLY
# longer than ``NETWORK_MAX_SECONDS`` we exit, exactly like the Conflict
# path, letting Docker's ``restart: unless-stopped`` respawn a clean instance
# that opens a fresh getUpdates connection. Contiguity is judged by
# ``NETWORK_GAP_SECONDS``: a quiet gap longer than that proves a poll
# succeeded in between (recovery), so the outage clock restarts — sporadic
# blips on a healthy idle bot never accumulate toward the threshold.
NETWORK_MAX_SECONDS = 180.0
NETWORK_GAP_SECONDS = 45.0
_network_first_seen: float | None = None
_network_last_seen: float | None = None

# Liveness watchdog — the safety net above assumes the getUpdates coroutine
# eventually RAISES something. It doesn't always: a VPN-pipeline exit
# rotation (a mandatory background job on the host; it ticks every 5 min
# and actually swaps the egress server every few days) can tear down the
# proxy's upstream mid-long-poll and leave the read wedged with NO
# exception ever surfacing. Observed 2026-07-06: both bots went
# alive-but-deaf for ~30h with zero further log lines — the NetworkError
# counter above never moved because it never saw an error to accumulate.
# A cheap heartbeat task proves the event loop is still scheduling
# coroutines; the check itself runs on a SEPARATE OS thread, because if
# the event loop is the thing that's actually stuck, an asyncio-scheduled
# timeout would never fire either.
LIVENESS_TICK_SECONDS = 15.0
LIVENESS_MAX_STALE_SECONDS = 90.0
_last_heartbeat: float = 0.0


async def _heartbeat_loop() -> None:
    """Cheap proof of forward progress: tick a shared timestamp."""
    global _last_heartbeat
    while True:
        _last_heartbeat = time.monotonic()
        await asyncio.sleep(LIVENESS_TICK_SECONDS)


def _liveness_watchdog_tick() -> None:
    """One check: force-exit if the heartbeat has gone stale too long.

    Split out from the sleep loop so tests can call it directly.
    """
    stale = time.monotonic() - _last_heartbeat
    if stale > LIVENESS_MAX_STALE_SECONDS:
        logger.critical(
            "Event loop unresponsive for %.0fs (no heartbeat) — forcing "
            "exit so the supervisor restarts a clean instance.",
            stale,
        )
        _terminate_for_sustained_conflict()


def _liveness_watchdog_loop() -> None:
    """Runs on its own OS thread, deliberately NOT asyncio — it must keep
    working even if the event loop itself is what's wedged."""
    while True:
        time.sleep(LIVENESS_TICK_SECONDS)
        _liveness_watchdog_tick()


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
    global _network_first_seen, _network_last_seen

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
        now = time.monotonic()
        # A quiet gap longer than NETWORK_GAP_SECONDS means a getUpdates
        # poll succeeded in between: the prior outage recovered, so reset
        # the outage clock instead of carrying stale elapsed time forward.
        if _network_last_seen is None or now - _network_last_seen > NETWORK_GAP_SECONDS:
            _network_first_seen = now
        _network_last_seen = now
        # _network_first_seen is always set in tandem with _network_last_seen
        # above; the fallback only satisfies the type checker on the
        # first-ever call (elapsed=0 is the correct fresh-start value anyway).
        first_seen = _network_first_seen if _network_first_seen is not None else now
        elapsed = now - first_seen
        text = f"transient network error: {err}"
        if text != _last_network_err_text:
            logger.info("%s", text)
            _last_network_err_text = text
        if elapsed >= NETWORK_MAX_SECONDS:
            logger.critical(
                "Sustained getUpdates network failure (%.0fs, no successful "
                "poll) — long-poll not recovering. Exiting so the supervisor "
                "restarts a clean instance.",
                elapsed,
            )
            _terminate_for_sustained_conflict()
        return
    _last_network_err_text = None
    _network_first_seen = None
    _network_last_seen = None
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


# Compatibility facade for extracted startup/shutdown and route registration.
_LIFECYCLE_STATE_NAMES = (
    "session_monitor",
    "_conflict_app",
    "_status_poll_task",
    "_card_timer_task",
    "_quota_alerts_task",
    "_metrics_flush_task",
    "_heartbeat_task",
    "_auth_preflight_task",
    "_usage_prewarm_task",
    "_send_file_relay_task",
    "_last_heartbeat",
)

for _state_name in _LIFECYCLE_STATE_NAMES:
    if hasattr(_lifecycle_impl, _state_name):
        globals()[_state_name] = getattr(_lifecycle_impl, _state_name)

_ORIGINAL_POST_INIT = _lifecycle_impl.post_init
_ORIGINAL_POST_SHUTDOWN = _lifecycle_impl.post_shutdown
_ORIGINAL_CREATE_BOT = _routes_impl.create_bot

_APP_FACADE_INTERNALS = {
    "_ModuleType",
    "_lifecycle_impl",
    "_routes_impl",
    "_LIFECYCLE_STATE_NAMES",
    "_ORIGINAL_POST_INIT",
    "_ORIGINAL_POST_SHUTDOWN",
    "_ORIGINAL_CREATE_BOT",
    "_APP_FACADE_INTERNALS",
    "_sync_app_implementation",
    "_pull_lifecycle_state",
    "_state_name",
}


def _sync_app_implementation(module: _ModuleType) -> None:
    """Push current, possibly monkeypatched facade names downstream."""
    facade_names = {
        name: value
        for name, value in globals().items()
        if not name.startswith("__") and name not in _APP_FACADE_INTERNALS
    }
    vars(module).update(facade_names)


def _pull_lifecycle_state() -> None:
    """Reflect lifecycle assignments back onto the canonical facade."""
    for name in _LIFECYCLE_STATE_NAMES:
        if hasattr(_lifecycle_impl, name):
            globals()[name] = getattr(_lifecycle_impl, name)


async def post_init(application: "Application[Any, Any, Any, Any, Any, Any]") -> None:
    _sync_app_implementation(_lifecycle_impl)
    try:
        await _ORIGINAL_POST_INIT(application)
    finally:
        _pull_lifecycle_state()


async def post_shutdown(
    application: "Application[Any, Any, Any, Any, Any, Any]",
) -> None:
    _sync_app_implementation(_lifecycle_impl)
    try:
        await _ORIGINAL_POST_SHUTDOWN(application)
    finally:
        _pull_lifecycle_state()


def create_bot() -> "Application[Any, Any, Any, Any, Any, Any]":
    _sync_app_implementation(_routes_impl)
    return _ORIGINAL_CREATE_BOT()
