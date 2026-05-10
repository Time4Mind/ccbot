"""Token-usage tracker — reads Claude Code transcripts and aggregates totals.

DM-multisession spec section 5: usage is sourced from the assistant turns in
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl; each assistant message
records ``usage.input_tokens``, ``usage.output_tokens``, and cache totals.

We do not maintain incremental in-memory counters — /status reads the
JSONL files at call time and computes the rolling 5h and weekly aggregates.
For typical session sizes this is sub-second; if it becomes hot we can move
to the same byte-offset approach used by SessionMonitor.

Public API:
  parse_session_usage(file_path) -> list[Turn]
  aggregate_session(sess) -> SessionUsage
  compute_user_usage(user_id) -> UserUsage
  format_usage_breakdown_compact(...) — render the live /usage modal block
  format_usage_status(...) — /status text body
  pop_session_token_alert(sess, user_id) -> int | None
      next per-session token threshold this session has just crossed
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiofiles

from .config import config
from .session import Session, session_manager

logger = logging.getLogger(__name__)


SECONDS_5H = 5 * 3600
SECONDS_WEEK = 7 * 86400


@dataclass
class Turn:
    """One assistant turn with its cost."""

    timestamp: float  # unix seconds
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SessionUsage:
    session_id: str  # Session.id, not claude_session_id
    name: str
    tokens_total: int = 0
    tokens_5h: int = 0
    tokens_weekly: int = 0


@dataclass
class UserUsage:
    sessions: list[SessionUsage] = field(default_factory=list)
    tokens_5h: int = 0
    tokens_weekly: int = 0
    tokens_total: int = 0


def _parse_iso(ts: str) -> float:
    """Parse Claude transcript ISO timestamps to unix seconds, 0 on failure."""
    if not ts:
        return 0.0
    try:
        # Python 3.11+ accepts trailing Z directly; <3.11 requires offset form.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


async def parse_session_usage(file_path: Path) -> list[Turn]:
    """Read a session JSONL and emit one Turn per assistant message with usage."""
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
                # Skip non-assistant messages.
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


async def aggregate_session(sess: Session) -> SessionUsage:
    """Compute total / 5h / weekly token usage for a single Session."""
    out = SessionUsage(session_id=sess.id, name=sess.name or sess.id)
    if not sess.claude_session_id or not sess.workdir:
        out.tokens_total = sess.token_usage_total  # fall back to persisted counter
        return out
    file_path = session_manager._build_session_file_path(
        sess.claude_session_id, sess.workdir
    )
    if file_path is None:
        out.tokens_total = sess.token_usage_total
        return out
    turns = await parse_session_usage(file_path)
    now = time.time()
    for t in turns:
        out.tokens_total += t.total
        if t.timestamp and (now - t.timestamp) <= SECONDS_5H:
            out.tokens_5h += t.total
        if t.timestamp and (now - t.timestamp) <= SECONDS_WEEK:
            out.tokens_weekly += t.total
    return out


async def compute_user_usage(user_id: int) -> UserUsage:
    """Aggregate usage across the user's live and recently-archived sessions.

    Sessions further back than the weekly window contribute to ``tokens_total``
    (per-session) but not to the rolling aggregates.
    """
    usage = UserUsage()
    sessions = session_manager.list_user_sessions(
        user_id, states=("active", "idle", "archived", "completed", "lost")
    )
    for sess in sessions:
        s = await aggregate_session(sess)
        usage.sessions.append(s)
        usage.tokens_total += s.tokens_total
        usage.tokens_5h += s.tokens_5h
        usage.tokens_weekly += s.tokens_weekly
        # Refresh cumulative counter on the Session record.
        if sess.token_usage_total != s.tokens_total:
            sess.token_usage_total = s.tokens_total
    session_manager._save_state()
    return usage


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

    if b.week_pct is not None and b.week_reset_hhmm:
        rate = _weekly_rate(b.week_pct, b.week_reset_hhmm)
        rows.append(
            f"{_quota_emoji(b.week_pct)} {t(user_id, 'usage.week')}: "
            f"{b.week_pct}%{rate} · {b.week_reset_hhmm}"
        )

    if b.week_sonnet_pct is not None and b.week_sonnet_reset_hhmm:
        rate = _weekly_rate(b.week_sonnet_pct, b.week_sonnet_reset_hhmm)
        rows.append(
            f"{_quota_emoji(b.week_sonnet_pct)} "
            f"{t(user_id, 'usage.week_sonnet')}: "
            f"{b.week_sonnet_pct}%{rate} · {b.week_sonnet_reset_hhmm}"
        )

    extra_label = t(user_id, "usage.on" if b.extra_enabled else "usage.off")
    rows.append(f"{t(user_id, 'usage.extra')}: {extra_label}")

    if not rows:
        return None
    return t(user_id, "usage.title") + "\n" + "\n".join(rows)


def format_usage_status(user_id: int, usage: UserUsage) -> str:
    """Render the /status text — per-session 5h tokens + lifetime totals.

    Sessions are split into Active / Archived / Lost groups; each line is
    name + 5h tokens + lifetime tokens, marked with ✓ for the active one.
    Real %-of-quota lives in the Menu→Status view (see
    ``format_usage_breakdown_compact``); /status text reports raw numbers.
    """
    del user_id
    lines: list[str] = []
    if usage.sessions:
        lines.append(
            f"*Tokens* — {usage.tokens_5h // 1000}k 5h · "
            f"{usage.tokens_weekly // 1000}k weekly · "
            f"{usage.tokens_total // 1000}k lifetime"
        )
    else:
        return "_No sessions yet._"

    active = session_manager.get_active_session(
        min(config.allowed_users) if config.allowed_users else 0
    )
    active_id = active.id if active else ""

    # Partition sessions by state so each group renders under its own header.
    by_state: dict[str, list[SessionUsage]] = {
        "active": [],
        "archived": [],
        "lost": [],
    }
    for s in usage.sessions:
        sess_obj = session_manager.get_session(s.session_id)
        state = sess_obj.state if sess_obj else "?"
        if state in ("active", "idle"):
            bucket = "active"
        elif state in ("archived", "completed"):
            bucket = "archived"
        elif state == "lost":
            bucket = "lost"
        else:
            continue
        by_state[bucket].append(s)

    section_headers = {
        "active": "*Active*",
        "archived": "*Archived*",
        "lost": "*Lost*",
    }
    for bucket in ("active", "archived", "lost"):
        rows = by_state[bucket]
        if not rows:
            continue
        lines.append("")
        lines.append(section_headers[bucket])
        for s in rows:
            marker = "✓" if s.session_id == active_id else " "
            lines.append(
                f"{marker} *{s.name}* — "
                f"{s.tokens_5h // 1000}k 5h / {s.tokens_total // 1000}k total"
            )
    return "\n".join(lines)


# --- Per-session token alerts ---


def _user_token_thresholds(user_id: int) -> list[int]:
    """User-configurable session-token alert thresholds, ascending order."""
    settings = session_manager.get_user_settings(user_id)
    raw = settings.get("session_token_alerts")
    if not isinstance(raw, list) or len(raw) != 3:
        raw = list(config.session_token_alert_defaults)
    out: list[int] = []
    for v in raw:
        try:
            out.append(max(0, int(v)))
        except (TypeError, ValueError):
            out.append(0)
    return sorted(t for t in out if t > 0)


def pop_session_token_alert(sess: Session, user_id: int) -> int | None:
    """Return the next per-session token threshold this session crossed, once.

    Mutates ``sess.alerted_token_thresholds`` to remember which thresholds
    have already fired. Caller is expected to persist the Session record.
    Returns the integer threshold (e.g. 100_000) or None.
    """
    thresholds = _user_token_thresholds(user_id)
    if not thresholds:
        return None
    fired: set[int] = set(sess.alerted_token_thresholds or [])
    for th in thresholds:
        if th in fired:
            continue
        if sess.token_usage_total >= th:
            fired.add(th)
            sess.alerted_token_thresholds = sorted(fired)
            return th
    return None
