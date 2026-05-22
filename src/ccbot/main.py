"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().

Also enforces a single-bot mutex via flock on ``$CCBOT_DIR/ccbot.lock``
— Telegram's getUpdates long-poll is exclusive per token, so a second
instance silently steals updates and the original starts logging
``Conflict: terminated by other getUpdates request`` until one dies.
The flock makes the second instance refuse to start instead.

Exit-code contract (the supervisor / restart scripts agree with it):
  * ``EXIT_CLEAN`` (0) — clean stop (``run_polling`` returned on
    SIGTERM/SIGINT) OR a clean *yield* because another healthy instance
    already holds the singleton lock. A yield is NOT a crash: the
    supervisor must back off quietly and re-probe, never restart-promptly.
    The process gate is owned by the supervisor, which probes the lock
    before launching; this in-process refuse is only a last-resort race
    guard for the window where two starts overlap.
  * ``EXIT_CRASH`` (1) — a real failure worth retrying / surfacing:
    misconfiguration (missing token), or any unexpected error. The
    supervisor restarts promptly after its base backoff.

The low-level ``_acquire_singleton_lock`` helper still raises
``SystemExit(1)`` on contention (its documented "could not acquire"
signal); ``main`` catches that and re-exits ``EXIT_CLEAN`` so the
operational refuse path looks like a yield, not a crash.
"""

import fcntl
import logging
import sys
from pathlib import Path
from typing import IO, Any

# Process-level exit-code contract — see module docstring. The supervisor
# treats EXIT_CLEAN as "do not restart-promptly" and EXIT_CRASH as
# "restart after the base backoff".
EXIT_CLEAN = 0
EXIT_CRASH = 1

# Held at module scope so the OS keeps the flock for the whole process
# lifetime. Local-scope file handles would be GC-closed once main()
# returns from acquiring them.
_singleton_lock_handle: IO[Any] | None = None


def _acquire_singleton_lock(lock_path: Path) -> IO[Any]:
    """Acquire an exclusive flock on ``lock_path`` or ``sys.exit(1)``.

    Returns the file handle holding the lock; callers MUST keep the
    handle alive for the process lifetime (we assign it to
    ``_singleton_lock_handle`` for this). ``FD_CLOEXEC`` is set so the
    lock doesn't leak into ``subprocess`` / ``asyncio.subprocess``
    children — a stray child outliving the parent would otherwise hold
    the lock and block future bot starts.

    On contention this raises ``SystemExit(1)`` — the helper's low-level
    "could not acquire" signal. ``main`` translates that into the clean
    yield (``EXIT_CLEAN``); the raw helper keeps the ``1`` so callers /
    tests can distinguish "acquired" from "someone else holds it".
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    fcntl.fcntl(fh.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Logging may not be configured yet on this path, so go via
        # stderr too — the supervisor wrapper captures it either way.
        msg = (
            f"Another ccbot instance holds {lock_path}. "
            "Refusing to start to avoid Telegram getUpdates conflict."
        )
        logging.getLogger(__name__).error(msg)
        print(f"Error: {msg}", file=sys.stderr)
        fh.close()
        raise SystemExit(1)
    return fh


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    from .logging_setup import configure_logging

    configure_logging()
    logging.getLogger().setLevel(logging.WARNING)

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    # PROCESS GATE — the singleton lock has to land BEFORE any network /
    # getMe / run_polling work: it is the authoritative liveness check
    # (flock auto-releases on holder death, so "held" ⟺ "a live holder
    # exists"). The supervisor probes this same lock before launching, so
    # reaching here with it held is a last-resort race guard, not the
    # routine path. We YIELD CLEANLY (EXIT_CLEAN) rather than crash:
    # another healthy instance owns the bot, and the supervisor must back
    # off quietly instead of restart-promptly.
    global _singleton_lock_handle
    from .utils import ccbot_dir

    try:
        _singleton_lock_handle = _acquire_singleton_lock(ccbot_dir() / "ccbot.lock")
    except SystemExit:
        logging.getLogger(__name__).info(
            "Another healthy ccbot instance holds the singleton lock; yielding cleanly."
        )
        sys.exit(EXIT_CLEAN)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    # run_polling installs SIGTERM/SIGINT handlers and shuts the
    # Application down gracefully before returning. When it returns, the
    # process exits and the OS closes ``_singleton_lock_handle`` — which
    # releases the flock. restart.sh polls for that release before
    # launching a replacement (bug A2d), so a clean-shutdown marker in the
    # log makes the boundary observable between the old and new instance.
    application.run_polling(allowed_updates=["message", "callback_query"])
    logger.info("Telegram bot stopped; releasing singleton lock and exiting.")


if __name__ == "__main__":
    main()
