"""Background poll of ``/context`` per live session.

Per pivot #43 follow-up: the user wants the *real* context-fill
percentage as reported by Claude Code's own ``/context`` command —
not derived from JSONL token math (which guessed at the model's
context-window denominator and got it wrong on extended-window
models).

The loop fires every ``CONTEXT_POLL_INTERVAL`` seconds and, for each
user's active / idle session:

  1. Skip if the pane shows a status spinner (``parse_status_line``
     non-None) — claude is busy, sending ``/context`` would queue it
     mid-turn and potentially fight whatever's running.
  2. Send ``/context`` to the tmux pane.
  3. Poll the pane for up to 4 s looking for the
     ``<n>k/<n>k tokens (<pct>%)`` line.
  4. Send Escape to dismiss the modal.
  5. If a pct was parsed, stash it on the live card state and the
     bg-status entry for this session, then refresh the panel for
     the user once at the end.

Archived / lost / completed sessions are skipped — they have no live
tmux window anyway, and the user explicitly said "НЕ архивных и НЕ
lost".
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from telegram import Bot

from ..config import config
from ..session import session_manager
from ..terminal_parser import parse_status_line
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


# How often to refresh context-pct for every live session. The user
# asked for 5-10 min — 8 min splits the difference, env-overridable.
CONTEXT_POLL_INTERVAL = float(os.getenv("CCBOT_CONTEXT_POLL_INTERVAL", "480"))

# Per-session pacing: 200 ms × _CONTEXT_POLL_STEPS = up to 4 s waiting
# for the modal to render after sending /context. The modal usually
# paints in well under a second.
_CONTEXT_POLL_STEPS = 20
_CONTEXT_POLL_TICK = 0.2

# Inter-session settle so we don't fire /context into N panes in the
# same wall-clock second when the user has many live sessions.
_INTER_SESSION_DELAY = 1.0


_CONTEXT_LINE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[kKmM]?\s*/\s*(\d+(?:\.\d+)?)\s*[kKmM]?\s+tokens\s*\((\d+)\s*%\)"
)


def _parse_context_pct(pane_text: str) -> int | None:
    """Pull the percentage from a ``X.Yk/Zk tokens (N%)`` line."""
    m = _CONTEXT_LINE.search(pane_text)
    if not m:
        return None
    try:
        return int(m.group(3))
    except (TypeError, ValueError):
        return None


async def _capture_with_scrollback(window_id: str) -> str | None:
    """Read ~100 lines of scrollback so the /context body stays visible
    even on narrow panes where the modal can be taller than the
    viewport."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-S",
            "-100",
            "-t",
            window_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("context_poll capture failed: %s", e)
    return None


async def _read_context_pct(window_id: str) -> int | None:
    """Send /context, poll for the percentage, dismiss with Escape.

    Returns the parsed percentage or None when:
      - the window vanished,
      - the pane is busy (spinner detected) — we'd be queuing /context
        mid-turn, which is risky,
      - the modal didn't paint within the budget.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return None
    pre = await tmux_manager.capture_pane(w.window_id)
    if pre and parse_status_line(pre) is not None:
        # Claude is mid-turn — defer to next interval. Sending /context
        # now would queue it and the subsequent Escape might land on
        # something else.
        return None

    pct: int | None = None
    try:
        await tmux_manager.send_keys(w.window_id, "/context")
        for _ in range(_CONTEXT_POLL_STEPS):
            await asyncio.sleep(_CONTEXT_POLL_TICK)
            pane_text = await _capture_with_scrollback(w.window_id)
            if not pane_text:
                continue
            pct = _parse_context_pct(pane_text)
            if pct is not None:
                break
    finally:
        # Always dismiss — leaving the modal up would block the user
        # the next time they look at the pane.
        try:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
        except Exception as e:
            logger.debug("context_poll dismiss failed: %s", e)
    return pct


async def context_poll_loop(bot: Bot) -> None:
    """Run forever, refreshing context-fill % for every live session."""
    logger.info("context_poll_loop started interval=%.0fs", CONTEXT_POLL_INTERVAL)
    # Stagger the first run a bit so we don't pile onto a fresh-boot
    # claude startup.
    await asyncio.sleep(30.0)
    while True:
        try:
            await _one_pass(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("context_poll pass crashed: %s", e)
        await asyncio.sleep(CONTEXT_POLL_INTERVAL)


async def _one_pass(bot: Bot) -> None:
    """Hit every live session once for every allowed user."""
    from . import bg_status
    from .notifications import refresh_panel, set_card_context_pct

    for user_id in config.allowed_users:
        # ``states`` filter excludes archived/completed/lost — per the
        # user's explicit ask. Iterate sequentially so we don't burst
        # /context into many panes at once.
        sessions = session_manager.list_user_sessions(
            user_id, states=("active", "idle")
        )
        touched = False
        for sess in sessions:
            if not sess.window_id:
                continue
            try:
                pct = await _read_context_pct(sess.window_id)
            except Exception as e:
                logger.debug("context_poll read failed sess=%s: %s", sess.id, e)
                pct = None
            if pct is not None:
                set_card_context_pct(user_id, sess.id, pct)
                bg_status.set_context_pct(user_id, sess.id, pct)
                touched = True
                logger.info(
                    "context_poll sess=%s pct=%d",
                    sess.id,
                    pct,
                    extra={
                        "event": "context_poll_update",
                        "user_id": user_id,
                        "session_id": sess.id,
                        "pct": pct,
                    },
                )
            await asyncio.sleep(_INTER_SESSION_DELAY)
        if touched:
            try:
                await refresh_panel(bot, user_id)
            except Exception as e:
                logger.debug("context_poll refresh_panel failed: %s", e)
