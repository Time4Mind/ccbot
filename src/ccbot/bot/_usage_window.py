"""Dedicated long-lived tmux window for /usage queries.

Spinning up a separate ``ccbot-usage`` window with its own Claude Code
process means the Status screen never has to interrupt whatever the
user's active session is doing — and ``/usage`` is a UI-only modal so
running it in a parked window costs nothing token-wise.

Public API:
  fetch_claude_usage() -> UsageInfo | None
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


USAGE_WINDOW_NAME = "ccbot-usage"
_usage_window_lock = asyncio.Lock()


async def _ensure_usage_window() -> str | None:
    """Find or lazily create the long-lived ccbot-usage window.

    Survives bot restarts (tmux server is the source of truth). On first
    creation we wait ~4s for Claude to reach its prompt before returning.
    """
    w = await tmux_manager.find_window_by_name(USAGE_WINDOW_NAME)
    if w:
        return w.window_id
    home = str(Path.home())
    success, message, _wname, wid = await tmux_manager.create_window(
        home, window_name=USAGE_WINDOW_NAME, start_claude=True
    )
    if not success:
        logger.debug("ensure_usage_window: create failed: %s", message)
        return None
    await asyncio.sleep(4.0)
    return wid


async def fetch_claude_usage() -> object | None:
    """Open /usage in the dedicated window, return the parsed UsageInfo.

    The modal frame paints instantly but the Current-session/week rows
    populate asynchronously (Claude shows a "Loading usage data…"
    placeholder first), so we keep polling until at least one quota row
    resolves or the 5 s budget runs out. Returns ``None`` on failure.
    """
    from ..terminal_parser import extract_usage_breakdown, parse_usage_output

    async with _usage_window_lock:
        wid = await _ensure_usage_window()
        if not wid:
            return None
        info = None
        try:
            await tmux_manager.send_keys(wid, "/usage")
            for _ in range(25):  # 25 × 200 ms = 5 s
                await asyncio.sleep(0.2)
                pane_text = await tmux_manager.capture_pane(wid)
                if not pane_text:
                    continue
                candidate = parse_usage_output(pane_text)
                if not candidate or not candidate.parsed_lines:
                    continue
                info = candidate
                breakdown = extract_usage_breakdown(candidate)
                if (
                    breakdown.session_pct is not None
                    or breakdown.week_pct is not None
                    or breakdown.week_sonnet_pct is not None
                ):
                    break
            try:
                await tmux_manager.send_keys(wid, "Escape", enter=False, literal=False)
            except Exception as e:
                logger.debug("fetch_claude_usage: dismiss failed: %s", e)
        except Exception as e:
            logger.debug("fetch_claude_usage: tmux failed: %s", e)
            return None
    return info
