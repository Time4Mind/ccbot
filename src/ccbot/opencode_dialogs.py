"""Finish OpenCode's extra model-variant dialog without asking Telegram users."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from .opencode_runtime import interactive_panel

if TYPE_CHECKING:
    from .session_models import Session

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


async def settle_variant(sess: Session | None, pane: str) -> bool:
    """Choose Default only in the exact native variant picker; never approve tools."""
    if sess is None:
        return False
    panel = interactive_panel(pane)
    if panel is None or panel[0] != "OpenCodeVariant" or sess.backend != "opencode":
        return False
    from .terminal_runtime import capture_session_pane, send_session_key

    key = f"{sess.node_id}:{sess.worker_session_id or sess.window_id}"
    async with _locks.setdefault(key, asyncio.Lock()):
        # Polling and the callback may observe the same dialog. A second
        # resolver must not send Enter into a composer that already resumed.
        current = interactive_panel(await capture_session_pane(sess))
        if current is None:
            return True
        if current[0] != "OpenCodeVariant":
            return False
        if re.search(r"(?m)^\s*(?:[●◉›>]\s*)?Default\s*$", current[1]):
            if not await send_session_key(sess, "Home"):
                return False
            return await send_session_key(sess, "Enter")
        # A filtered picker may contain old misrouted text. Dismiss it rather
        # than approving another option; the model itself is already selected.
        return await send_session_key(sess, "Escape")
