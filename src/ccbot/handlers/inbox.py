"""Inbound media inbox — stores uploaded files under a session's workdir.

Spec section 7 (I1 + I2 + TTL 24h):
  - Photos and documents from the active session land in
    `<workdir>/.ccbot-inbox/<utc-ts>-<filename>`.
  - The active session's tmux receives a synthetic notice so Claude can
    discover the file via path.
  - A periodic sweep removes inbox files older than INBOX_TTL_HOURS.

Public API:
  save_inbox_file(workdir, filename, fetch) -> Path
  ccbot_inbox_dir(workdir) -> Path
  inbox_sweep() -> int
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from ..config import config
from ..session import session_manager

logger = logging.getLogger(__name__)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Sanitize a Telegram-supplied file name for filesystem use."""
    name = name.strip().replace(" ", "_")
    name = _SAFE_NAME_RE.sub("_", name)
    if not name:
        return "file"
    if len(name) > 80:
        head, dot, ext = name.rpartition(".")
        name = head[: 80 - len(ext) - 1] + dot + ext if dot else name[:80]
    return name


def ccbot_inbox_dir(workdir: str) -> Path:
    """Return (and ensure) the .ccbot-inbox directory for a session's workdir."""
    inbox = Path(workdir).expanduser().resolve() / config.inbox_dirname
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


async def save_inbox_file(
    workdir: str,
    filename: str,
    fetch: Callable[[Path], Awaitable[None]],
) -> Path:
    """Save an inbound file to the session's inbox.

    `fetch` is an async callable that downloads the bytes to the given path.
    Returns the path that was written.
    """
    inbox = ccbot_inbox_dir(workdir)
    ts = int(time.time())
    target = inbox / f"{ts}-{_safe_filename(filename)}"
    await fetch(target)
    return target


def inbox_sweep() -> int:
    """Delete inbox files older than INBOX_TTL_HOURS across all known sessions.

    Returns the number of files removed.
    """
    ttl_seconds = config.inbox_ttl_hours * 3600
    if ttl_seconds <= 0:
        return 0
    now = time.time()
    removed = 0
    seen: set[Path] = set()
    for sess in session_manager.sessions.values():
        if not sess.workdir:
            continue
        try:
            inbox = Path(sess.workdir) / config.inbox_dirname
        except (OSError, ValueError):
            continue
        if inbox in seen or not inbox.is_dir():
            continue
        seen.add(inbox)
        for entry in inbox.iterdir():
            try:
                if not entry.is_file():
                    continue
                age = now - entry.stat().st_mtime
                if age > ttl_seconds:
                    entry.unlink()
                    removed += 1
            except OSError as e:
                logger.debug("inbox_sweep skip %s: %s", entry, e)
    if removed:
        logger.info(
            "inbox_sweep removed %d files older than %.0fh",
            removed,
            config.inbox_ttl_hours,
        )
    return removed
