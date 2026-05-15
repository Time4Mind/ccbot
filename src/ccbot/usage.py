"""Context-size reader for live sessions.

Reads each Claude Code transcript JSONL on demand and computes the
**latest** assistant turn's full input-token count (incl. cache reads).
That number divided by ``CONTEXT_BUDGET`` (200 000 by default) is the
"how full is the context window" percentage shown on the live card —
both for the active session (above the bg panel) and per-row for bg
sessions in the panel itself.

Public API:
  parse_session_usage(file_path) -> list[Turn]
      back-compat parser used by tests; sums input + output, ignores
      cache fields.
  context_pct_for_session(sess) -> int | None
      latest-turn context size / 200k, in %, or None when no data.
  format_usage_breakdown_compact(user_id, info) -> str | None
      renders the live /usage modal block (active session header
      glyphs come from this).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiofiles

from . import session_claude_io
from .session import Session, session_manager

logger = logging.getLogger(__name__)


# Claude context window in tokens, used as the denominator for the
# "context: N%" display on the live card. Claude Code on Claude 4.x
# (Opus 4.7 / Sonnet 4.6) runs with the extended-window beta enabled
# by default — observed cache_read alone routinely exceeds 200k, which
# is impossible under the 200k limit. 1M is the right denominator for
# Claude-Code-driven sessions today. Override via env if running
# elsewhere.
CONTEXT_BUDGET = int(os.getenv("CCBOT_CONTEXT_BUDGET", "1000000"))


@dataclass
class Turn:
    """One assistant turn with its cost (back-compat for tests)."""

    timestamp: float  # unix seconds
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def _parse_iso(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


async def parse_session_usage(file_path: Path) -> list[Turn]:
    """Read a session JSONL and emit one Turn per assistant message with usage.

    Kept for back-compat with the existing test suite; current code
    paths use :func:`context_pct_for_session` instead.
    """
    turns: list[Turn] = []
    if not file_path.exists():
        return turns
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                usage = msg.get("usage") or {}
                inp = int(usage.get("input_tokens", 0) or 0)
                out = int(usage.get("output_tokens", 0) or 0)
                if inp == 0 and out == 0:
                    continue
                ts = _parse_iso(obj.get("timestamp", ""))
                turns.append(Turn(timestamp=ts, input_tokens=inp, output_tokens=out))
    except OSError as e:
        logger.debug("usage: cannot read %s: %s", file_path, e)
    return turns


async def _last_assistant_context_tokens(file_path: Path) -> int | None:
    """Read JSONL and return the latest assistant turn's full context size
    (input_tokens + cache_creation + cache_read). None if no data."""
    if not file_path.exists():
        return None
    last: int | None = None
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                usage = msg.get("usage") or {}
                inp = int(usage.get("input_tokens", 0) or 0)
                cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
                cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                total = inp + cc + cr
                if total > 0:
                    last = total
    except OSError as e:
        logger.debug("usage: cannot read %s: %s", file_path, e)
    return last


async def context_pct_for_session(sess: Session) -> int | None:
    """Latest assistant turn's context size as % of ``CONTEXT_BUDGET``.

    Returns None when no JSONL yet (fresh session, before first turn).
    Bumps to 100 % on overflow rather than reporting absurd values —
    the user just needs to know it's full.
    """
    if not sess.claude_session_id or not sess.workdir:
        return None
    file_path = session_claude_io.build_session_file_path(
        sess.claude_session_id, sess.workdir
    )
    if file_path is None:
        return None
    tokens = await _last_assistant_context_tokens(file_path)
    if tokens is None:
        return None
    pct = int(round(tokens * 100 / CONTEXT_BUDGET))
    return max(0, min(100, pct))


# --- /usage modal compact renderer (Menu→Status) ---


_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _parse_hhmm(hh_mm: str) -> tuple[int, int] | None:
    try:
        h_str, m_str = hh_mm.split(":", 1)
        hour = int(h_str)
        minute = int(m_str)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour, minute


def _hours_until_clock(hh_mm: str) -> float | None:
    """Hours from now until the next occurrence of a 24h HH:MM wall-clock."""
    parsed = _parse_hhmm(hh_mm)
    if parsed is None:
        return None
    hour, minute = parsed
    from datetime import datetime, timedelta

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 3600.0


def _days_until_weekday_clock(weekday_key: str, hh_mm: str) -> float | None:
    """Days from now until the next occurrence of the given weekday + HH:MM."""
    target_weekday = _WEEKDAY_INDEX.get(weekday_key)
    if target_weekday is None:
        return None
    parsed = _parse_hhmm(hh_mm)
    if parsed is None:
        return None
    hour, minute = parsed
    from datetime import datetime, timedelta

    now = datetime.now()
    days_ahead = (target_weekday - now.weekday()) % 7
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
        days=days_ahead
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds() / 86400.0


def _quota_emoji(pct: int) -> str:
    """Stoplight glyph for an at-a-glance quota signal."""
    if pct < 50:
        return "🟢"
    if pct < 75:
        return "🟡"
    if pct < 90:
        return "🟠"
    return "🔴"


def format_usage_breakdown_compact(user_id: int, info: object) -> str | None:
    """Render the live /usage payload as a compact, multilingual block.

    Each row: ``label: pct% · rate · resetTime`` (rate is %/h for the 5h
    window; the weekly rows omit it because the modal doesn't expose the
    elapsed time within the week). Returns None when info has nothing.
    """
    from .i18n import t
    from .terminal_parser import UsageInfo, extract_usage_breakdown

    if not isinstance(info, UsageInfo):
        return None
    b = extract_usage_breakdown(info)
    rows: list[str] = []

    # 5h: pct + hourly burn rate + reset time.
    if b.session_pct is not None and b.session_reset_hhmm:
        hours_left = _hours_until_clock(b.session_reset_hhmm)
        rate_str = ""
        if hours_left is not None:
            elapsed = max(0.1, 5.0 - min(5.0, hours_left))
            rate = b.session_pct / elapsed
            rate_str = f" · {rate:.1f}%/h"
        rows.append(
            f"{_quota_emoji(b.session_pct)} {t(user_id, 'usage.5h')}: "
            f"{b.session_pct}%{rate_str} · {b.session_reset_hhmm}"
        )

    # Weekly window: %/d burn rate using user-configured reset day.
    weekly_day = session_manager.get_user_settings(user_id).get(
        "weekly_reset_day", "mon"
    )

    def _weekly_rate(pct: int, reset_hhmm: str) -> str:
        days_left = _days_until_weekday_clock(weekly_day, reset_hhmm)
        if days_left is None:
            return ""
        elapsed = max(0.1, 7.0 - min(7.0, days_left))
        return f" · {pct / elapsed:.1f}%/d"

    if b.week_pct is not None:
        if b.week_reset_hhmm:
            rate = _weekly_rate(b.week_pct, b.week_reset_hhmm)
            tail = f"{rate} · {b.week_reset_hhmm}"
        else:
            tail = ""
        rows.append(
            f"{_quota_emoji(b.week_pct)} {t(user_id, 'usage.week')}: "
            f"{b.week_pct}%{tail}"
        )

    if b.week_sonnet_pct is not None:
        if b.week_sonnet_reset_hhmm:
            rate = _weekly_rate(b.week_sonnet_pct, b.week_sonnet_reset_hhmm)
            tail = f"{rate} · {b.week_sonnet_reset_hhmm}"
        else:
            tail = ""
        rows.append(
            f"{_quota_emoji(b.week_sonnet_pct)} "
            f"{t(user_id, 'usage.week_sonnet')}: "
            f"{b.week_sonnet_pct}%{tail}"
        )

    extra_label = t(user_id, "usage.on" if b.extra_enabled else "usage.off")
    rows.append(f"{t(user_id, 'usage.extra')}: {extra_label}")

    if not rows:
        return None
    return t(user_id, "usage.title") + "\n" + "\n".join(rows)
