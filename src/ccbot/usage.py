"""Token-usage tracker — reads Claude Code transcripts and aggregates totals.

DM-multisession spec section 5: usage is sourced from the assistant turns in
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl; each assistant message
records ``usage.input_tokens``, ``usage.output_tokens``, and cache totals.

We do not maintain incremental in-memory counters in v0.1 — /status reads the
JSONL files at call time and computes the rolling 5h and weekly aggregates.
For typical session sizes this is sub-second; if it becomes hot we can move
to the same byte-offset approach used by SessionMonitor.

Public API:
  parse_session_usage(file_path) -> list[Turn]
  aggregate_session(sess) -> SessionUsage
  compute_user_usage(user_id) -> UserUsage
  notify_quota_crossings(...) — called after a session emits a complete turn

The 5h and weekly budgets are read from config (MAX_5H_TOKENS, MAX_WEEKLY_TOKENS).
Per-session 5h budget for soft warnings is SESSION_TOKEN_BUDGET_5H.
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


def format_usage_status(user_id: int, usage: UserUsage) -> str:
    """Render the /status text. Per-session lines marked with active emoji."""
    del user_id
    lines: list[str] = []
    cap_5h = max(1, config.max_5h_tokens)
    cap_w = max(1, config.max_weekly_tokens)
    pct_5h = int(100 * usage.tokens_5h / cap_5h)
    pct_w = int(100 * usage.tokens_weekly / cap_w)
    lines.append("*Usage (Max x20)*")
    lines.append(
        f"5h window: {pct_5h:>3}% ({usage.tokens_5h // 1000}k / {cap_5h // 1000}k est)"
    )
    lines.append(
        f"Weekly:    {pct_w:>3}% ({usage.tokens_weekly // 1000}k / "
        f"{cap_w // 1000}k est)"
    )
    lines.append("")
    if usage.sessions:
        lines.append("*Sessions*")
        # Show active first
        active = session_manager.get_active_session(
            min(config.allowed_users) if config.allowed_users else 0
        )
        active_id = active.id if active else ""
        for s in usage.sessions:
            sess_obj = session_manager.get_session(s.session_id)
            state = sess_obj.state if sess_obj else "?"
            marker = "✓" if s.session_id == active_id else " "
            lines.append(
                f"{marker} `{s.session_id}` *{s.name}* ({state}) — "
                f"{s.tokens_5h // 1000}k 5h / {s.tokens_total // 1000}k total"
            )
    return "\n".join(lines)


# --- Quota crossings ---

# Per-session "warning emitted" set; kept in memory (best-effort).
# Each entry is a session_id whose 75% threshold was already announced this 5h window.
_warned_sessions: set[str] = set()


def reset_quota_warnings() -> None:
    """Drop all 'already warned' markers — call when 5h window resets."""
    _warned_sessions.clear()


def should_warn_quota(session_usage: SessionUsage) -> bool:
    """Return True iff this session just crossed 75% of SESSION_TOKEN_BUDGET_5H
    and we have not yet warned about this crossing.
    """
    cap = config.session_token_budget_5h
    if cap <= 0:
        return False
    if session_usage.tokens_5h * 4 < cap * 3:  # less than 75%
        return False
    if session_usage.session_id in _warned_sessions:
        return False
    _warned_sessions.add(session_usage.session_id)
    return True
