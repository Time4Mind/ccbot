"""Background poll of the live /usage modal — push on threshold crossings.

A single shared task runs every ``QUOTA_ALERT_POLL_INTERVAL`` (default 5
minutes), reuses the dedicated ccbot-usage tmux window via
``fetch_claude_usage``, and emits one push per (quota, threshold)
transition. The thresholds match the at-a-glance emoji bands in
``usage._quota_emoji`` so the bot reaches the same conclusion the user
would by looking at the modal.

State is process-local: the task remembers the last "level" it observed
for each of the three quota rows; on level increase it fires once.
Levels reset when the modal advertises a fresh window (we detect that
heuristically as ``pct`` going down past the previous level).

Public API:
  quota_alerts_loop(bot) -> never returns; intended for asyncio.create_task
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telegram import Bot

from ..config import config
from ..i18n import t
from ..terminal_parser import UsageInfo, extract_usage_breakdown
from .message_sender import safe_send

logger = logging.getLogger(__name__)


# Same boundaries as ``usage._quota_emoji`` — change both together.
THRESHOLDS: tuple[int, ...] = (50, 75, 90)


def _level_for_pct(pct: int) -> int:
    """Return the highest threshold index this pct has crossed (0..len)."""
    level = 0
    for th in THRESHOLDS:
        if pct >= th:
            level += 1
    return level


@dataclass
class _QuotaState:
    """Last observed level per row, kept across polls."""

    session: int = 0
    week: int = 0
    week_sonnet: int = 0


_state = _QuotaState()


def _label(user_id: int, key: str) -> str:
    """i18n label for the quota row, with a sensible fallback."""
    return t(user_id, key)


async def _emit_push(bot: Bot, user_id: int, label: str, pct: int) -> None:
    """Send the user a one-line crossing notification for `label` at `pct`%."""
    emoji = "🔴" if pct >= 90 else "🟠" if pct >= 75 else "🟡"
    body = f"{emoji} {label}: {pct}%"
    try:
        await safe_send(bot, user_id, body)
    except Exception as e:
        logger.debug("quota alert push failed: %s", e)


async def _poll_once(bot: Bot, *, suppress_push: bool = False) -> None:
    """One iteration: fetch, diff levels, push transitions per allowed user.

    With ``suppress_push=True`` the state is updated to whatever the modal
    reports without sending notifications — used on first poll so the bot
    doesn't spam already-crossed thresholds when it starts up.
    """
    from ..bot._usage_window import fetch_claude_usage

    info = await fetch_claude_usage()
    if not isinstance(info, UsageInfo):
        return
    b = extract_usage_breakdown(info)

    rows: list[tuple[str, int | None, str]] = [
        ("session", b.session_pct, "usage.5h"),
        ("week", b.week_pct, "usage.week"),
        ("week_sonnet", b.week_sonnet_pct, "usage.week_sonnet"),
    ]

    for attr, pct, label_key in rows:
        if pct is None:
            continue
        new_level = _level_for_pct(pct)
        old_level = getattr(_state, attr)
        if new_level == old_level:
            continue
        setattr(_state, attr, new_level)
        if suppress_push or new_level <= old_level:
            continue
        for user_id in config.allowed_users:
            await _emit_push(bot, user_id, _label(user_id, label_key), pct)


async def quota_alerts_loop(bot: Bot) -> None:
    """Run forever, polling every ``QUOTA_ALERT_POLL_INTERVAL`` seconds."""
    interval = max(60.0, config.quota_alert_poll_interval)
    logger.info("Quota alerts loop started (interval %.0fs)", interval)
    # Warm the state on first poll without spamming — capture the current
    # level as the baseline so we only push *transitions* later.
    try:
        await _poll_once(bot, suppress_push=True)
    except Exception as e:
        logger.debug("initial quota poll failed: %s", e)

    while True:
        try:
            await asyncio.sleep(interval)
            await _poll_once(bot)
        except asyncio.CancelledError:
            logger.info("Quota alerts loop cancelled")
            raise
        except Exception as e:
            logger.warning("quota alerts loop iteration failed: %s", e)
