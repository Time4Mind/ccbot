"""Prewarmed default-session lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .session_models import reserve_owner, Session
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

_locks: dict[int, asyncio.Lock] = {}
_replacement_tasks: set[asyncio.Task[Session | None]] = set()
RECONCILE_SECONDS = 5.0
_START_RETRY_SECONDS = 30.0
_MAX_START_RETRY_SECONDS = 300.0
_start_retries: dict[int, tuple[tuple[str, str | None], int, float]] = {}


def _defer_failed_start(user_id: int, key: tuple[str, str | None]) -> float:
    previous = _start_retries.get(user_id)
    failures = previous[1] + 1 if previous and previous[0] == key else 1
    delay = min(
        _MAX_START_RETRY_SECONDS,
        _START_RETRY_SECONDS * 2 ** min(failures - 1, 4),
    )
    _start_retries[user_id] = (key, failures, time.monotonic() + delay)
    return delay


def _settings(user_id: int) -> dict[str, Any]:
    from .session import session_manager

    return session_manager.get_user_settings(user_id)


def _backend(user_id: int, settings: dict[str, Any]) -> str | None:
    from .session import session_manager

    enabled = list(session_manager.get_effective_backends(user_id, "local"))
    selected = str(settings.get("default_session_backend") or "")
    return selected if selected in enabled else enabled[0] if enabled else None


def get_default_reserve(user_id: int) -> Session | None:
    from .session import session_manager

    candidates = [
        sess
        for sess in session_manager.sessions.values()
        if reserve_owner(sess) == user_id
        and sess.state in ("active", "idle")
        and sess.window_id
    ]
    return min(candidates, key=lambda sess: sess.created_at) if candidates else None


def _claimed_name(workdir: str, claimed_id: str) -> str:
    from .session import session_manager

    base = Path(workdir).name or "session"
    used = {
        sess.name
        for sess in session_manager.sessions.values()
        if sess.id != claimed_id and sess.name
    }
    number = 1
    while f"{base}-{number}" in used:
        number += 1
    return f"{base}-{number}"


async def ensure_default_session(bot: Any, user_id: int) -> Session | None:
    """Ensure exactly one empty prewarmed reserve exists for ``user_id``."""
    from .session import session_manager

    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        settings = _settings(user_id)
        enabled = bool(settings.get("default_session_enabled", False))
        directory = str(settings.get("default_session_directory") or "").strip()
        backend = _backend(user_id, settings)
        reserves = [
            sess
            for sess in session_manager.sessions.values()
            if reserve_owner(sess) == user_id
        ]
        keep = next(
            (
                sess
                for sess in reserves
                if enabled
                and sess.state in ("active", "idle")
                and sess.window_id
                and sess.workdir == directory
                and sess.backend == backend
            ),
            None,
        )
        for stale in reserves:
            if stale is keep:
                continue
            session_manager.cancel_window_startup(stale.window_id)
            if stale.window_id:
                try:
                    await tmux_manager.kill_window(stale.window_id)
                except Exception as exc:
                    logger.warning(
                        "Could not stop stale default reserve %s: %s", stale.id, exc
                    )
            session_manager.delete_session(stale.id)
        if keep is not None:
            _start_retries.pop(user_id, None)
            if keep.default_reserve_user_id != user_id:
                keep.mark_default_reserve(user_id)
                session_manager.save_state()
                logger.info(
                    "Recovered rollback-compatible default reserve user=%d session=%s",
                    user_id,
                    keep.id,
                )
            await _remove_empty_default_orphans(user_id, directory, backend, keep)
            return keep
        if not enabled:
            _start_retries.pop(user_id, None)
            return None
        if not directory or not Path(directory).is_dir() or backend is None:
            logger.warning(
                "Default session not created user=%d directory=%r backend=%r",
                user_id,
                directory,
                backend,
            )
            return None

        orphan = next(
            (
                sess
                for sess in session_manager.sessions.values()
                if _is_empty_default_orphan(sess, directory, backend)
            ),
            None,
        )
        if orphan is not None:
            _start_retries.pop(user_id, None)
            orphan.mark_default_reserve(user_id)
            session_manager.save_state()
            logger.info(
                "Adopted empty orphan as default reserve user=%d session=%s",
                user_id,
                orphan.id,
            )
            return orphan

        retry_key = (directory, backend)
        retry = _start_retries.get(user_id)
        if retry is not None and retry[0] == retry_key and time.monotonic() < retry[2]:
            return None
        if retry is not None and retry[0] != retry_key:
            _start_retries.pop(user_id, None)
        try:
            (
                success,
                message,
                _window_name,
                window_id,
            ) = await tmux_manager.create_window(
                directory,
                backend=backend,
                wait_for_codex_ready=backend == "codex",
            )
        except Exception as exc:
            success, message, window_id = False, type(exc).__name__, ""
        if not success or not window_id:
            delay = _defer_failed_start(user_id, retry_key)
            logger.error(
                "Default session start failed user=%d backend=%s directory=%s "
                "retry_in_seconds=%.0f: %s",
                user_id,
                backend,
                directory,
                delay,
                message,
            )
            return None
        _start_retries.pop(user_id, None)
        session_manager.mark_window_starting(
            window_id,
            backend=backend,
            resume=False,
            bot=bot,
            user_id=user_id,
        )
        sess = session_manager.create_session(
            name="default",
            window_id=window_id,
            workdir=directory,
            backend=backend,
            default_reserve_user_id=user_id,
        )
        if session_manager.get_active_session(user_id) is None:
            session_manager.set_active_session(user_id, sess.id)
        session_manager.save_state()
        logger.info(
            "Default reserve created user=%d session=%s window=%s backend=%s",
            user_id,
            sess.id,
            window_id,
            backend,
        )
        return sess


async def default_session_loop(bot: Any, user_ids: tuple[int, ...]) -> None:
    """Cheap supervisor: recreate a missing reserve after external tmux loss."""
    while True:
        for user_id in user_ids:
            try:
                await ensure_default_session(bot, user_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Default-session reconciliation failed user=%d: %s",
                    user_id,
                    exc,
                )
        await asyncio.sleep(RECONCILE_SECONDS)


def claim_default_session(bot: Any, user_id: int, sess: Session) -> bool:
    """Atomically turn a reserve into an ordinary session before queueing input."""
    from .session import session_manager

    if reserve_owner(sess) != user_id:
        return False
    sess.clear_default_reserve()
    sess.name = _claimed_name(sess.workdir, sess.id)
    session_manager.save_state()
    task = asyncio.create_task(
        ensure_default_session(bot, user_id),
        name=f"default-session-replacement:{user_id}",
    )
    _replacement_tasks.add(task)
    task.add_done_callback(_replacement_tasks.discard)
    logger.info(
        "Default reserve claimed user=%d session=%s replacement_scheduled=true",
        user_id,
        sess.id,
    )
    return True


def _is_empty_default_orphan(
    sess: Session, directory: str, backend: str | None
) -> bool:
    return (
        reserve_owner(sess) == 0
        and sess.name == "default"
        and sess.state in ("active", "idle")
        and bool(sess.window_id)
        and sess.workdir == directory
        and sess.backend == backend
        and sess.message_count == 0
        and not sess.claude_session_id
        and not sess.pending_preprocessing
        and not sess.preprocessed_prompt_hashes
    )


async def _remove_empty_default_orphans(
    user_id: int, directory: str, backend: str | None, keep: Session
) -> None:
    """Remove empty duplicates left by a rollback to a marker-unaware version."""
    from .session import session_manager

    for sess in list(session_manager.sessions.values()):
        if sess is keep or not _is_empty_default_orphan(sess, directory, backend):
            continue
        session_manager.cancel_window_startup(sess.window_id)
        try:
            await tmux_manager.kill_window(sess.window_id)
        except Exception as exc:
            logger.warning("Could not stop orphan default reserve %s: %s", sess.id, exc)
        session_manager.delete_session(sess.id)
        logger.info(
            "Removed empty orphan default reserve user=%d session=%s",
            user_id,
            sess.id,
        )


def reset_default_session_tasks_for_test() -> None:
    for task in tuple(_replacement_tasks):
        task.cancel()
    _replacement_tasks.clear()
    _locks.clear()
    _start_retries.clear()


async def shutdown_default_session_tasks() -> None:
    """Cancel only in-process reconciliation jobs; tmux reserves persist."""
    tasks = tuple(_replacement_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _replacement_tasks.clear()
