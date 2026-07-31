"""Per-user /usage modal renderer + per-session context-fill from JSONL.

Context-fill % per session is computed from the JSONL transcript —
sending the live ``/context`` command into the pane writes the modal
output back into the JSONL (as a fake user turn) AND eats real tokens
from claude's own context window. JSONL math is non-invasive.

Per-model denominator:
  * Claude 4.x (opus-4-*, sonnet-4-*) — 1 000 000 (extended context
    is the Claude Code default for these models)
  * Claude 3.x and unknown — 200 000

Public API:
  parse_session_usage(file_path) -> list[Turn]
      back-compat parser used by tests; sums input + output, ignores
      cache fields.
  context_pct_for_session(sess) -> int | None
      latest assistant turn's full input size (incl. cache reads)
      divided by the per-model budget, clamped to ``[0, 100]``.
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

from . import session_claude_io
from .config import config
from .session import Session, session_manager
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


def _budget_for_model(model: str) -> int:
    """Per-model context-window denominator in tokens.

    Default is 1M — current Claude model families (Opus 4.x, Sonnet 4.x)
    ship with the extended window. The only family that stays on 200k is
    Haiku (4.5 and earlier), so we route any model name containing
    ``haiku`` to the 200k bucket and let everything else fall through
    to 1M. Unknown / empty model names default to 1M.
    """
    if not model:
        return 1_000_000
    if "haiku" in model.lower():
        return 200_000
    return 1_000_000


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


async def context_pct_for_session(sess: Session) -> int | None:
    """Latest assistant turn's full context size as % of the model's
    budget. None when there's no JSONL data yet.

    "Full context size" = ``input_tokens + cache_creation_input_tokens
    + cache_read_input_tokens`` of the most recent assistant message.
    The model name is read from that same message and routed through
    :func:`_budget_for_model` — 200k for Haiku, 1M for everything else.
    """
    if not sess.claude_session_id or not sess.workdir:
        return None
    file_path = session_claude_io.build_session_file_path(
        sess.claude_session_id, sess.workdir
    )
    if file_path is None or not file_path.exists():
        return None
    last_total: int | None = None
    last_model: str = ""
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
                    last_total = total
                    last_model = msg.get("model", "") or last_model
    except OSError as e:
        logger.debug("context_pct: cannot read %s: %s", file_path, e)
        return None
    if last_total is None:
        return None
    budget = _budget_for_model(last_model)
    pct = int(round(last_total * 100 / budget))
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


def _daily_quota_budget(
    used_percent: int,
    resets_at: int | None,
    state: dict[str, object] | None,
    *,
    now: datetime | None = None,
) -> tuple[float, dict[str, object]] | None:
    """Return today's remaining budget and the updated persistent state.

    At the first observation of each local calendar day, the remaining weekly
    quota is divided equally over every calendar day through the reset date.
    That day's allocation then stays fixed while usage is subtracted from it.
    Over- or underspend is therefore redistributed only when the next day
    begins.
    """
    if resets_at is None:
        return None
    current = now or datetime.now()
    reset = datetime.fromtimestamp(resets_at)
    if reset <= current:
        return None

    today_key = current.date().isoformat()
    saved = state or {}
    same_window = saved.get("resets_at") == resets_at
    same_day = saved.get("date") == today_key
    try:
        day_start_used = float(saved["day_start_used"])
        daily_budget = float(saved["daily_budget"])
    except (KeyError, TypeError, ValueError):
        same_day = False

    # A lower percentage means Codex reset the window even if its advertised
    # reset timestamp has not changed yet.
    if not same_window or not same_day or used_percent < day_start_used:
        calendar_days = max(1, (reset.date() - current.date()).days + 1)
        day_start_used = float(used_percent)
        daily_budget = max(0.0, 100.0 - used_percent) / calendar_days

    spent_today = max(0.0, used_percent - day_start_used)
    remaining_today = daily_budget - spent_today
    new_state: dict[str, object] = {
        "resets_at": resets_at,
        "date": today_key,
        "day_start_used": day_start_used,
        "daily_budget": daily_budget,
    }
    return remaining_today, new_state


def _persisted_daily_quota_budget(
    used_percent: int, resets_at: int | None
) -> float | None:
    """Calculate today's budget and persist its daily baseline."""
    state_file = config.config_dir / "codex_quota_day.json"
    state: dict[str, object] | None = None
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state = raw
    except (OSError, json.JSONDecodeError):
        pass

    result = _daily_quota_budget(used_percent, resets_at, state)
    if result is None:
        return None
    remaining_today, new_state = result
    try:
        atomic_write_json(state_file, new_state)
    except OSError as e:
        logger.debug("usage: cannot persist daily quota budget: %s", e)
    return remaining_today


def format_usage_breakdown_compact(user_id: int, info: object) -> str | None:
    """Render the live /usage payload as a compact, multilingual block.

    Each row: ``label: pct% · rate · resetTime`` (rate is %/h for the 5h
    window; the weekly rows omit it because the modal doesn't expose the
    elapsed time within the week). Returns None when info has nothing.
    """
    from .codex_usage import CodexUsageInfo
    from .i18n import t
    from .terminal_parser import UsageInfo, extract_usage_breakdown

    if isinstance(info, CodexUsageInfo):
        from datetime import datetime

        rows: list[str] = []

        if info.five_hour is None:
            rows.append(
                f"⚪ {t(user_id, 'usage.5h')}: {t(user_id, 'usage.not_reported')}"
            )
        else:
            window = info.five_hour
            rows.append(f"{_quota_emoji(window.used_percent)} {t(user_id, 'usage.5h')}")
            rows.append(f"{t(user_id, 'usage.used')}: {window.used_percent}%")
            if window.resets_at is not None:
                reset = datetime.fromtimestamp(window.resets_at).strftime("%H:%M")
                rows.append(f"{t(user_id, 'usage.reset')}: {reset}")
        if info.weekly is not None:
            window = info.weekly
            rows.append(f"{_quota_emoji(window.used_percent)} {t(user_id, 'usage.week')}")
            rows.append(f"{t(user_id, 'usage.used')}: {window.used_percent}%")
            today_budget = _persisted_daily_quota_budget(
                window.used_percent, window.resets_at
            )
            if today_budget is not None:
                if today_budget >= 0:
                    value = f"{t(user_id, 'usage.today_left')} {today_budget:.1f}%"
                else:
                    value = (
                        f"{t(user_id, 'usage.today_overspent')} "
                        f"{abs(today_budget):.1f}%"
                    )
                rows.append(
                    f"{t(user_id, 'usage.today')}: {value}"
                )
            if window.resets_at is not None:
                reset = datetime.fromtimestamp(window.resets_at).strftime("%d.%m %H:%M")
                rows.append(f"{t(user_id, 'usage.reset')}: {reset}")
        if not rows:
            return None
        return t(user_id, "usage.title.codex") + "\n\n" + "\n\n".join(rows)

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
