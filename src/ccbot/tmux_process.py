"""Process cleanup helpers for tmux-backed agent sessions.

The caller supplies OS and subprocess functions so tmux_manager keeps its
historical monkeypatch seams while this leaf owns PID parsing and signalling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


def kill_orphan_processes(
    agent_session_id: str,
    backend: str,
    *,
    run: Callable[..., Any],
    kill: Callable[[int, int], None],
    own_pid: int,
    parent_pid: int,
    sigterm: int,
    timeout_error: type[BaseException],
    logger: logging.Logger,
) -> int:
    """Signal surviving agent resume processes and return the count."""
    normalized_backend = backend.strip().lower()
    if normalized_backend == "claude":
        pattern = f"claude.*--resume[ =]{agent_session_id}"
    elif normalized_backend == "codex":
        pattern = f"codex.*resume[ ]+{agent_session_id}"
    else:
        logger.warning(
            "kill_orphan_agent_processes: unsupported backend %r, skipping",
            backend,
        )
        return 0
    try:
        result = run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (timeout_error, OSError) as exc:
        logger.debug("pgrep failed: %s", exc)
        return 0

    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue

    killed = 0
    for pid in pids:
        if pid in (own_pid, parent_pid):
            continue
        try:
            kill(pid, sigterm)
            killed += 1
            logger.info(
                "kill_orphan_agent pid=%d backend=%s session=%s",
                pid,
                normalized_backend,
                agent_session_id,
            )
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            logger.warning("kill_orphan_agent pid=%d denied: %s", pid, exc)
    return killed
