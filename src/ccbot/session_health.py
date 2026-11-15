"""Detect exited local agents independently of terminal activity and turns.

A returned login shell is positive evidence of an exit. An unfamiliar
foreground process, an idle agent, or a failed probe is not crash evidence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from .session_models import Session, reserve_owner
from .tmux_manager import tmux_manager

if TYPE_CHECKING:
    from telegram import Bot

    from .tmux_manager import TmuxWindow

logger = logging.getLogger(__name__)
EXIT_CONFIRM_SECONDS = 2.0
STARTUP_GRACE_SECONDS = 15.0
_SHELLS = {"sh", "bash", "zsh", "dash", "ash", "fish", "ksh", "tcsh", "csh", "nu"}
_suspected: dict[tuple[str, str], float] = {}
_expected_stops: set[tuple[str, str]] = set()
_notice_retry: dict[tuple[int, str], float] = {}


def window_has_exited(window: TmuxWindow | None) -> bool:
    if window is None:
        return True
    command = getattr(window, "pane_current_command", "")
    return isinstance(command, str) and os.path.basename(command).lstrip("-") in _SHELLS


async def window_agent_alive(window_id: str) -> bool | None:
    """None means the probe failed; never destroy a reserve on uncertainty."""
    try:
        window = await asyncio.wait_for(
            tmux_manager.find_window_by_id(window_id), timeout=3.0
        )
        if not window_has_exited(window):
            return True
        if window is None:
            return False
        # A foreground shell may itself be an agent tool or an updater. Only
        # an empty pane process tree proves that control returned to the shell.
        children = await pane_has_children(window_id)
        return children if children is not None else None
    except Exception as exc:
        logger.warning(
            "session_health_probe_failed window=%s error=%s",
            window_id,
            type(exc).__name__,
        )
        return None


async def _read_process_command(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        return proc.returncode or 0, output.decode(errors="replace")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def pane_has_children(window_id: str) -> bool | None:
    """Inspect only PIDs/parent PIDs; command arguments may contain secrets."""
    code, output = await _read_process_command(
        "tmux", "display-message", "-p", "-t", window_id, "#{pane_pid}"
    )
    if code or not output.strip().isdigit():
        return None
    root = int(output.strip())
    code, output = await _read_process_command("ps", "-eo", "pid=,ppid=")
    if code:
        return None
    rows = [line.split() for line in output.splitlines()]
    if not any(len(row) == 2 and row[0] == str(root) for row in rows):
        return None
    return any(len(row) == 2 and row[1] == str(root) for row in rows)


def expect_session_stop(sess: Session) -> None:
    """Called before the first await of an intentional archive/kill."""
    _expected_stops.add((sess.id, sess.window_id))


def _starting(sess: Session) -> bool:
    from .session import session_manager

    return (
        session_manager.is_window_starting(sess.window_id)
        and time.time() - max(sess.activated_at, sess.created_at)
        < STARTUP_GRACE_SECONDS
    )


def exit_reason(pane: str, *, missing: bool = False) -> str:
    """Persist an allowlisted explanation, never raw terminal output/secrets."""
    if missing:
        return "window_missing"
    tail = " ".join(pane.splitlines()[-16:]).casefold()
    if "workspace routing discovery" in tail and "timed out" in tail:
        return "routing_timeout"
    if "account/read failed" in tail:
        return "account_read"
    if any(
        text in tail
        for text in ("authentication failed", "unauthorized", "token expired")
    ):
        return "auth"
    if any(
        text in tail for text in ("segmentation fault", "core dumped", "fatal error")
    ):
        return "crash"
    return "agent_exited"


async def check_session_processes(
    bot: Bot, windows: dict[str, TmuxWindow], panes: dict[str, str], *, now: float
) -> None:
    """Use the poller's fresh process snapshot, even when panes never change."""
    from .config import config
    from .handlers.cleanup import clear_session_state
    from .session import session_manager

    live_keys: set[tuple[str, str]] = set()
    for sess in list(session_manager.sessions.values()):
        if (
            sess.node_id != "local"
            or sess.state not in ("active", "idle")
            or not sess.window_id
        ):
            continue
        wid = sess.window_id
        key = sess.id, wid
        live_keys.add(key)
        if (
            key in _expected_stops
            or _starting(sess)
            or not window_has_exited(windows.get(wid))
        ):
            _suspected.pop(key, None)
            continue
        first = _suspected.setdefault(key, now)
        if now - first < EXIT_CONFIRM_SECONDS:
            continue
        # Recheck immediately before changing durable state. A process can
        # restart, or a window can be rebound, between snapshots.
        if await window_agent_alive(wid) is not False:
            _suspected.pop(key, None)
            continue
        if (
            sess.window_id != wid
            or sess.state not in ("active", "idle")
            or key in _expected_stops
        ):
            continue
        reason = exit_reason(panes.get(wid, ""), missing=wid not in windows)
        is_reserve = bool(reserve_owner(sess))
        if not is_reserve:
            sess.exit_reason = reason
            sess.exit_notified_users.clear()
        session_manager.cancel_window_startup(wid)
        # A crash must not silently route the next request into another task.
        session_manager.mark_session_lost(sess.id, fallback=False)
        _suspected.pop(key, None)
        if is_reserve:
            # Empty reserves have no user history to inspect in the old shell.
            await tmux_manager.kill_window(wid)
        logger.warning(
            "session_process_exited session=%s window=%s reserve=%s reason=%s",
            sess.id,
            wid,
            is_reserve,
            reason,
        )
        for user_id in sorted(config.allowed_users):
            try:
                await clear_session_state(user_id, wid, bot)
            except Exception as exc:
                logger.debug(
                    "session_exit_ui_cleanup_failed session=%s error=%s",
                    sess.id,
                    type(exc).__name__,
                )
    for key in _suspected.keys() - live_keys:
        _suspected.pop(key, None)
    _expected_stops.intersection_update(live_keys)
    await deliver_exit_notices(bot, now=now)


async def deliver_exit_notices(bot: Bot, *, now: float) -> None:
    """Retry unsent notices; persisted acknowledgement prevents restart spam."""
    from .config import config
    from .handlers.message_sender import safe_send
    from .handlers.notifications import refresh_session_keyboard
    from .i18n import t
    from .session import session_manager

    for user_id in sorted(config.allowed_users):
        for sess in list(session_manager.sessions.values()):
            key = user_id, sess.id
            if (
                sess.state != "lost"
                or sess.node_id != "local"
                or not sess.exit_reason
                or user_id in sess.exit_notified_users
                or reserve_owner(sess)
            ):
                continue
            if now < _notice_retry.get(key, 0.0):
                continue
            try:
                sent = await safe_send(
                    bot,
                    user_id,
                    t(
                        user_id,
                        "session.exit.notice",
                        name=sess.name,
                        reason=t(user_id, f"session.exit.{sess.exit_reason}"),
                    ),
                )
                if sent is None:
                    _notice_retry[key] = now + 30.0
                    continue
            except Exception as exc:
                _notice_retry[key] = now + 30.0
                logger.warning(
                    "session_exit_notice_failed session=%s error=%s",
                    sess.id,
                    type(exc).__name__,
                )
                continue
            sess.exit_notified_users.append(user_id)
            session_manager.save_state()
            _notice_retry.pop(key, None)
            try:
                await refresh_session_keyboard(bot, user_id)
            except Exception:
                logger.debug("session_exit_keyboard_refresh_failed session=%s", sess.id)
