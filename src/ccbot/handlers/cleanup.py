"""Unified cleanup API for session state.

Provides centralized cleanup that coordinates state across modules,
preventing memory leaks when a session is archived, killed, or its
tmux window vanishes externally.

Functions:
  - clear_session_state: Clean up all in-memory state for a (user, window) pair.
"""

from telegram import Bot

from .interactive_ui import clear_interactive_for_window, clear_interactive_msg
from ..session_models import Session


async def clear_session_state(
    user_id: int,
    window_id: str,
    bot: Bot | None = None,
) -> None:
    """Clear in-memory state for a (user, window) pair.

    Called when:
      - A session is archived (auto-idle TTL or `/done`/`/kill`).
      - A tmux window vanishes externally.
    """
    if bot is not None:
        await clear_interactive_msg(user_id, bot, window_id)
    else:
        clear_interactive_for_window(user_id, window_id)


async def teardown_session_runtime(
    user_id: int,
    sess: Session,
    bot: Bot,
) -> set[str]:
    """Stop every runtime bound to ``sess`` and remove stale provider mappings.

    A provider session can be mapped to more than one tmux window after a failed
    resume.  Archiving only ``sess.window_id`` leaves another Codex writer alive
    and makes every later restore fail with ``already has an active writer``.
    """
    # Import lazily to keep cleanup.py free of the session manager import cycle.
    from ..session import session_manager
    from ..tmux_manager import tmux_manager

    provider_session_id = sess.claude_session_id
    window_ids = {sess.window_id} if sess.window_id else set()
    if provider_session_id:
        window_ids.update(
            await session_manager.remove_provider_session_bindings(provider_session_id)
        )

    async def stop_window(window_id: str) -> None:
        session_manager.cancel_window_startup(window_id)
        window = await tmux_manager.find_window_by_id(window_id)
        if window:
            await tmux_manager.kill_window(window.window_id)
        await clear_session_state(user_id, window_id, bot)

    for window_id in sorted(window_ids):
        await stop_window(window_id)

    if provider_session_id:
        await tmux_manager.kill_orphan_agent_processes(
            provider_session_id, sess.backend
        )
        # A startup hook can race the first removal while its tmux process is
        # being terminated.  A second exact purge closes that final binding.
        late_window_ids = await session_manager.remove_provider_session_bindings(
            provider_session_id
        )
        for window_id in sorted(late_window_ids - window_ids):
            await stop_window(window_id)
        window_ids.update(late_window_ids)
        # No provider process remains to emit another valid binding. Remove a
        # buffered hook write that may have landed while the late windows were
        # being stopped.
        buffered_window_ids = await session_manager.remove_provider_session_bindings(
            provider_session_id
        )
        for window_id in sorted(buffered_window_ids - window_ids):
            await stop_window(window_id)
        window_ids.update(buffered_window_ids)
        await session_manager.remove_provider_session_bindings(provider_session_id)
    return window_ids
