"""Background-session status panel — silent state for non-active sessions.

In DM mode background sessions don't emit any chat messages of their
own (no live card edits, no push). Instead this module keeps a per-user
map of their latest status, and ``render_panel`` formats a compact
block that the active session's card appends at its tail.

Status states (driven only by ``handle_new_message`` transitions):
  - "working"      ⏳  events arriving while session is bg
  - "finished"     ✅  terminal text turn (end_turn) while bg
  - "error"        ❌  error event while bg
  - "needs_action" ❓  AskUserQuestion / ExitPlanMode / permission
                       prompt detected on bg session

The pending interactive UI itself is detected and remembered here
(``pending_interactive_ui``) so the switcher-tap handler can render
the prompt+keyboard on the active carrier without re-running
terminal parsing.

Visibility:
  - A session appears in the panel only while it is bg (the active
    session is filtered out of ``render_panel``).
  - ``clear_for_user_session`` drops one user's entry — called when
    the user taps the session in the switcher (= switch into).
  - ``clear_for_session`` drops the entry across all users — called
    from archive/kill/done paths.

Persistence: ``serialize_per_user`` / ``load_per_user`` round-trip the
status map into ``state.json`` via SessionManager. ``pending_interactive_ui``
is NOT persisted — terminal_parser re-detects on the next poll cycle
after restart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..config import config

# ``..session`` imports back into this module from
# ``SessionManager._load_state`` to rehydrate the bg-status panel; a
# top-level import here would cycle through a half-built session_manager
# singleton during startup. All session lookups are done lazily inside
# the functions that need them.
if TYPE_CHECKING:
    from ..session import Session

logger = logging.getLogger(__name__)


Status = Literal["working", "finished", "error", "needs_action"]


_STATUS_EMOJI: dict[Status, str] = {
    "working": "⏳",
    "finished": "✅",
    "error": "❌",
    "needs_action": "❓",
}


@dataclass
class BgStatus:
    """One row in the bg-status panel.

    Attributes:
        status: latest status enum.
        last_change: wall-clock timestamp of the last status mutation,
            used to sort newest-first in the panel.
        pending_interactive_ui: snapshot of the interactive UI body
            seen on this session's tmux pane (and its UI name). Set
            together with ``status="needs_action"`` so the switcher tap
            can render the prompt without re-capturing the pane.
    """

    status: Status = "working"
    last_change: float = 0.0
    pending_interactive_ui: tuple[str, str] | None = None  # (content, ui_name)
    # Latest known context-fill percent for this session, sourced from
    # the JSONL transcript by ``usage.context_pct_for_session`` after each
    # assistant turn. Rendered as ``context N%`` in the panel row.
    context_pct: int | None = None


# Per-user, per-session BgStatus.
_bg: dict[int, dict[str, BgStatus]] = {}


def _entry(user_id: int, session_id: str) -> BgStatus:
    return _bg.setdefault(user_id, {}).setdefault(session_id, BgStatus())


def _touch(entry: BgStatus) -> None:
    entry.last_change = time.time()


def update_status(
    user_id: int,
    session_id: str,
    status: Status,
    *,
    interactive_ui: tuple[str, str] | None = None,
) -> bool:
    """Set status for one bg session. Returns True if the visible state
    changed (status emoji flipped) so callers can decide whether the
    active card needs a re-render.

    A first-call for an unknown session is always reported as a change.
    """
    is_new_entry = session_id not in _bg.get(user_id, {})
    entry = _entry(user_id, session_id)
    old_status = entry.status
    entry.status = status
    if status == "needs_action" and interactive_ui is not None:
        entry.pending_interactive_ui = interactive_ui
    elif status != "needs_action":
        entry.pending_interactive_ui = None
    _touch(entry)
    return is_new_entry or old_status != status


def get_pending_interactive_ui(user_id: int, session_id: str) -> tuple[str, str] | None:
    """Return (content, ui_name) snapshot for a bg session in needs_action,
    or None if not in that state."""
    entry = _bg.get(user_id, {}).get(session_id)
    if entry is None:
        return None
    return entry.pending_interactive_ui


def clear_pending_ui(user_id: int, session_id: str) -> bool:
    """Drop a stashed interactive-UI snapshot when the prompt is gone from
    the pane. Reverts ``status="needs_action"`` back to ``"working"`` so
    the badge doesn't claim "needs action" on a session that no longer
    has a prompt. Returns True if any visible state changed."""
    entry = _bg.get(user_id, {}).get(session_id)
    if entry is None or entry.pending_interactive_ui is None:
        return False
    entry.pending_interactive_ui = None
    if entry.status == "needs_action":
        entry.status = "working"
    _touch(entry)
    return True


def set_context_pct(user_id: int, session_id: str, pct: int) -> None:
    """Cache the latest context-fill % for a session's bg-panel row.
    Auto-creates the entry if missing — a fresh bg session that just
    received its first turn doesn't yet have a status entry, but we
    want the context number to show as soon as it's known.
    """
    _entry(user_id, session_id).context_pct = pct


async def infer_status_from_jsonl(sess: "Session") -> Status | None:
    """Read the last assistant turn from ``sess``'s JSONL and infer
    whether the session is currently working or has finished.

    Used when a session becomes background and we need to seed its
    panel row with a meaningful badge — the in-memory bg_status entry
    may be stale ("working" left over from before the user switched
    INTO it) or missing entirely (session finished while the bot was
    off, no JSONL event has fired since). Returns None when there's
    no JSONL to read.
    """
    # Lazy imports: top-level would cycle through SessionManager._load_state.
    from .. import session_claude_io

    if not sess.claude_session_id or not sess.workdir:
        return None
    file_path = session_claude_io.build_session_file_path(
        sess.claude_session_id, sess.workdir
    )
    if file_path is None or not file_path.exists():
        return None
    last_stop: str = ""
    last_role: str = ""
    import json as _json

    import aiofiles

    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                last_role = "assistant"
                last_stop = msg.get("stop_reason", "") or ""
    except OSError as e:
        logger.debug("infer_status: cannot read %s: %s", file_path, e)
        return None
    if last_role != "assistant":
        return None
    if last_stop in ("end_turn", "stop_sequence", "max_tokens"):
        return "finished"
    # ``tool_use`` stop_reason — session was mid-turn when the JSONL
    # last got a write. Treat as still working.
    return "working"


def clear_for_session(session_id: str) -> bool:
    """Drop the entry for ``session_id`` across all users. Called on
    archive / kill / done. Returns True if anything was dropped."""
    dropped = False
    for uid, bucket in list(_bg.items()):
        if session_id in bucket:
            bucket.pop(session_id, None)
            dropped = True
        if not bucket:
            _bg.pop(uid, None)
    return dropped


def clear_for_user_session(user_id: int, session_id: str) -> bool:
    """Drop the bg entry for one (user, session). Called when the user
    switches INTO this session — it's no longer "background" relative
    to them. Returns True if anything was dropped."""
    bucket = _bg.get(user_id)
    if not bucket or session_id not in bucket:
        return False
    bucket.pop(session_id, None)
    if not bucket:
        _bg.pop(user_id, None)
    return True


def _badge(sess: "Session", entry: BgStatus) -> str:
    """Render one panel row: ``<emoji> <name> <status_glyph> [context N%]``.

    The context-fill suffix sits AFTER all the status emoji so a glance
    parses status-first, fill-second (pivot #43 feedback). When pct is
    unknown, the suffix is omitted entirely.
    """
    from .switcher import session_emoji

    name = sess.name or sess.id
    sess_emoji = session_emoji(sess)
    status_glyph = _STATUS_EMOJI.get(entry.status, "")
    parts = [sess_emoji, name, status_glyph]
    line = " ".join(p for p in parts if p)
    if entry.context_pct is not None:
        line = f"{line} · context {entry.context_pct}%"
    return line


def render_panel(user_id: int, *, active_session_id: str = "") -> str:
    """Render the bg-status block for ``user_id``. Empty string when no
    background session has anything to report.

    The active session is never shown in the panel — its state already
    occupies the live card above. Entries are sorted newest-first
    (``last_change`` desc) and capped by ``config.bg_status_max``; the
    overflow tail collapses to a single ``+N more`` line.
    """
    bucket = _bg.get(user_id)
    if not bucket:
        return ""

    # Late import: top-level would cycle through SessionManager._load_state
    # which calls back into this module before session_manager is bound.
    from ..session import session_manager

    rows: list[tuple[float, str]] = []
    for sid, entry in bucket.items():
        if sid == active_session_id:
            continue
        sess = session_manager.get_session(sid)
        if sess is None:
            continue
        if sess.state not in ("active", "idle"):
            # Archived/lost shouldn't appear in panel; the lifecycle
            # path should have called ``clear_for_session`` but guard
            # anyway in case of a missed hook.
            continue
        rows.append((entry.last_change, _badge(sess, entry)))

    if not rows:
        return ""

    rows.sort(key=lambda r: r[0], reverse=True)
    cap = config.bg_status_max
    visible = [text for _, text in rows[:cap]]
    extra = max(0, len(rows) - cap)

    block = ["─── фон ───", *visible]
    if extra > 0:
        block.append(f"… +{extra} more")
    return "\n".join(block)


# --- Persistence helpers (called from SessionManager.save/load_state) ---


def serialize_per_user() -> dict[str, dict[str, dict[str, Any]]]:
    """Round-trip-safe representation for state.json. pending_interactive_ui
    is intentionally dropped — terminal_parser will re-detect on the next
    poll cycle, and persisting a stale screenshot of the prompt would be
    worse than re-rendering it fresh."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for uid, bucket in _bg.items():
        row: dict[str, dict[str, Any]] = {}
        for sid, entry in bucket.items():
            row[sid] = {
                "status": entry.status,
                "last_change": entry.last_change,
                "context_pct": entry.context_pct,
            }
        if row:
            out[str(uid)] = row
    return out


def load_per_user(raw: dict[str, Any] | None) -> None:
    """Populate the in-memory map from a state.json blob. Skip malformed
    entries silently — restart should never be blocked by a corrupted
    panel snapshot. Tolerates legacy ``quota_level``/``seen`` fields by
    ignoring them."""
    _bg.clear()
    if not raw:
        return
    for uid_str, bucket in raw.items():
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if not isinstance(bucket, dict):
            continue
        target = _bg.setdefault(uid, {})
        for sid, data in bucket.items():
            if not isinstance(data, dict):
                continue
            status_val = data.get("status", "working")
            if status_val not in ("working", "finished", "error", "needs_action"):
                continue
            try:
                last_change = float(data.get("last_change", 0.0))
            except (TypeError, ValueError):
                last_change = 0.0
            ctx_raw = data.get("context_pct")
            ctx_pct: int | None
            try:
                ctx_pct = int(ctx_raw) if ctx_raw is not None else None
            except (TypeError, ValueError):
                ctx_pct = None
            target[sid] = BgStatus(
                status=status_val,
                last_change=last_change,
                context_pct=ctx_pct,
            )
