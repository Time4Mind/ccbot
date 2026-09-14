"""Per-user /usage modal renderer + per-session context-fill from JSONL.

Context-fill % per session is computed from its JSONL transcript. Claude is
estimated from the latest assistant input usage; Codex exposes exact current
turn usage and the actual model window in ``token_count`` rollout events.
Reading either format is non-invasive.

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
    """Current session context size as % of its model budget.

    For Codex this uses the latest rollout ``token_count`` event's exact
    ``last_token_usage.total_tokens / model_context_window`` pair.
    None is returned when the session has no usable JSONL data yet.

    "Full context size" = ``input_tokens + cache_creation_input_tokens
    + cache_read_input_tokens`` of the most recent assistant message.
    The model name is read from that same message and routed through
    :func:`_budget_for_model` — 200k for Haiku, 1M for everything else.
    """
    if not sess.claude_session_id:
        return None
    if sess.backend == "codex":
        from . import codex_session_io

        file_path: Path | None = None
        if sess.window_id:
            window_state = session_manager.window_states.get(sess.window_id)
            if window_state and window_state.transcript_path:
                candidate = Path(window_state.transcript_path)
                if candidate.exists():
                    file_path = candidate
        if file_path is None:
            file_path = codex_session_io.build_session_file_path(
                sess.claude_session_id, sess.workdir
            )
        if file_path is None or not file_path.exists():
            return None
        context_fill = await codex_session_io.context_fill(file_path)
        if context_fill is None:
            return None
        used_tokens, context_window = context_fill
        pct = int(round(used_tokens * 100 / context_window))
        return max(0, min(100, pct))

    if not sess.workdir:
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
    day_start_used = float(used_percent)
    daily_budget = 0.0
    try:
        saved_day_start = saved["day_start_used"]
        saved_daily_budget = saved["daily_budget"]
        if not isinstance(saved_day_start, int | float) or not isinstance(
            saved_daily_budget, int | float
        ):
            raise TypeError
        day_start_used = float(saved_day_start)
        daily_budget = float(saved_daily_budget)
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


def format_usage_breakdown_compact(
    user_id: int,
    info: object,
    *,
    age_seconds: float = 0,
    refreshing: bool = False,
) -> str:
    """Render one compact quota table; absent values are always ``-``."""
    from .codex_usage import CodexUsageInfo
    from .i18n import t
    from .terminal_parser import UsageInfo, extract_usage_breakdown

    age_minutes = max(0, int(age_seconds // 60))
    title = t(user_id, "usage.status_age", age=age_minutes)
    if refreshing:
        title += " ⏳"
    headers = (
        t(user_id, "usage.column.cli"),
        t(user_id, "usage.column.5h"),
        t(user_id, "usage.column.week"),
        t(user_id, "usage.column.today"),
        t(user_id, "usage.column.reset"),
    )

    def table(rows: list[tuple[str, str, str, str, str]]) -> str:
        lines = ["| " + " | ".join(headers) + " |", "|---|---|---|---|---|"]
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        return f"*{title}*\n\n" + "\n".join(lines)

    if isinstance(info, CodexUsageInfo):
        five = f"{info.five_hour.used_percent}%" if info.five_hour else "-"
        week = f"{info.weekly.used_percent}%" if info.weekly else "-"
        today = "-"
        reset = "-"
        if info.weekly:
            budget = _persisted_daily_quota_budget(
                info.weekly.used_percent, info.weekly.resets_at
            )
            if budget is not None:
                today = f"{budget:.1f}%"
            if info.weekly.resets_at is not None:
                reset = datetime.fromtimestamp(info.weekly.resets_at).strftime(
                    "%d.%m %H:%M"
                )
        signal = max(
            (window.used_percent for window in (info.five_hour, info.weekly) if window),
            default=0,
        )
        return table([(_quota_emoji(signal) + " Codex", five, week, today, reset)])

    if not isinstance(info, UsageInfo):
        return table([("🔴 -", "-", "-", "-", "-")])
    b = extract_usage_breakdown(info)
    values = [pct for pct in (b.session_pct, b.week_pct) if pct is not None]
    signal = max(values, default=0)
    rows = [
        (
            _quota_emoji(signal) + " Claude",
            f"{b.session_pct}%" if b.session_pct is not None else "-",
            f"{b.week_pct}%" if b.week_pct is not None else "-",
            "-",
            b.week_reset_hhmm or "-",
        )
    ]
    if b.week_sonnet_pct is not None:
        rows.append(
            (
                _quota_emoji(b.week_sonnet_pct) + " Sonnet",
                "-",
                f"{b.week_sonnet_pct}%",
                "-",
                b.week_sonnet_reset_hhmm or "-",
            )
        )
    return table(rows)
