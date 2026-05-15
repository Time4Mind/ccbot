"""Per-user /usage modal renderer + back-compat JSONL parsers.

Context-fill % per session is sourced from Claude's own ``/context``
command via :mod:`ccbot.handlers.context_poll` (not from JSONL token
math — extended-window models render the JSONL approach unreliable).

Public API:
  parse_session_usage(file_path) -> list[Turn]
      back-compat parser used by tests; sums input + output, ignores
      cache fields.
  format_usage_breakdown_compact(user_id, info) -> str | None
      renders the live /usage modal block (Menu→Status / Anthropic
      quota glyphs).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiofiles

from .session import session_manager

logger = logging.getLogger(__name__)


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
