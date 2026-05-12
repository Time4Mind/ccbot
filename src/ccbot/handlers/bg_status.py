"""Background-session status panel — silent state for non-active sessions.

In DM mode background sessions don't emit any chat messages of their
own (no live card edits, no push). Instead this module keeps a per-user
map of their latest status, and ``render_panel`` formats a compact
block that the active session's card appends at its tail. The user
sees `[name] ⏳/✅/❌/❓` badges for every background session that has
something to report, sticky across active-card edits.

Status states (matches the user-approved emoji set):
  - "working"      ⏳  events arriving while session is bg
  - "finished"     ✅  terminal text turn while bg
  - "error"        ❌  error while bg (currently unused — events come
                       through the same paths as completion; left in
                       place for future hookups)
  - "needs_action" ❓  AskUserQuestion / ExitPlanMode / permission
                       prompt detected on bg session

Visibility:
  - A session appears in the panel only when status ∈ those four.
  - On user-driven switch INTO a session: ``mark_seen`` flips
    ``seen=True`` if the status is a finalised one (✅/❌/❓).
  - On user-driven switch AWAY (next ``mark_seen`` for another session):
    sessions whose ``seen=True`` AND status hasn't reverted to "working"
    are removed.
  - New event for a bg session resets ``seen=False`` and bumps the
    status accordingly (any event → "working", terminal → "finished",
    interactive UI → "needs_action").
  - ``clear_for_session`` is called from archive paths so a killed
    session doesn't leave a stale badge behind.

Quota indicator is a separate overlay (``quota_level``), updated by
``update_quota`` from the usage layer when a session crosses one of
``config.bg_status_quota_thresholds``. Levels are sticky downward —
once a session hits red it stays red for the lifetime of the badge.

The pending interactive UI itself is detected and remembered here too
(``pending_interactive_ui``) so the switcher-tap handler can render the
prompt+keyboard on the active carrier without re-running terminal
parsing.

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
QuotaLevel = Literal["none", "green", "yellow", "red"]


_STATUS_EMOJI: dict[Status, str] = {
    "working": "⏳",
    "finished": "✅",
    "error": "❌",
    "needs_action": "❓",
}

_QUOTA_EMOJI: dict[QuotaLevel, str] = {
    "none": "",
    "green": "⚠️🟢",
    "yellow": "⚠️🟡",
    "red": "⚠️🔴",
}


_QUOTA_ORDER: dict[QuotaLevel, int] = {
    "none": 0,
    "green": 1,
    "yellow": 2,
    "red": 3,
}


@dataclass
class BgStatus:
    """One row in the bg-status panel.

    Attributes:
        status: latest status enum.
        quota_level: latest quota threshold crossed, sticky upward.
        seen: True after the user tapped this session in the switcher
            while status was finalised. Cleared by any new "working"
            transition.
        last_change: monotonic timestamp of the last status mutation.
            Used for the ``BG_STATUS_MAX`` "newest first" trim.
        pending_interactive_ui: snapshot of the interactive UI body
            seen on this session's tmux pane (and its UI name). Set
            together with ``status="needs_action"`` so the switcher tap
            can render the prompt without re-capturing the pane.
    """

    status: Status = "working"
    quota_level: QuotaLevel = "none"
    seen: bool = False
    last_change: float = 0.0
    pending_interactive_ui: tuple[str, str] | None = None  # (content, ui_name)


# Per-user, per-session BgStatus.
_bg: dict[int, dict[str, BgStatus]] = {}


def _entry(user_id: int, session_id: str) -> BgStatus:
    return _bg.setdefault(user_id, {}).setdefault(session_id, BgStatus())


def _touch(entry: BgStatus) -> None:
    entry.last_change = time.time()


def has_panel_content(user_id: int) -> bool:
    """True if the user has any bg-status entry worth rendering."""
    return bool(_bg.get(user_id))


def update_status(
    user_id: int,
    session_id: str,
    status: Status,
    *,
    interactive_ui: tuple[str, str] | None = None,
) -> bool:
    """Set status for one bg session. Returns True if the visible state
    changed (status emoji or seen flag flipped) so callers can decide
    whether the active card needs a re-render.

    A first-call for an unknown session is always reported as a change —
    otherwise the very first ``working`` event after a switch-to-bg
    silently no-ops (default dataclass value is also ``working``) and
    the panel never gains the ⏳ badge until a status transition lands.
    """
    is_new_entry = session_id not in _bg.get(user_id, {})
    entry = _entry(user_id, session_id)
    old_status = entry.status
    old_seen = entry.seen
    entry.status = status
    # Any new event resets the "seen" flag — the user must look again.
    entry.seen = False
    if status == "needs_action" and interactive_ui is not None:
        entry.pending_interactive_ui = interactive_ui
    elif status != "needs_action":
        entry.pending_interactive_ui = None
    _touch(entry)
    return is_new_entry or old_status != status or old_seen != entry.seen


def update_quota(user_id: int, session_id: str, level: QuotaLevel) -> bool:
    """Lift quota level for one session. Sticky upward — once at red, stays
    red even if later usage measurements suggest otherwise (because the
    underlying transcript tokens-spent counter is monotonic). Returns
    True if the visible level changed."""
    entry = _entry(user_id, session_id)
    if _QUOTA_ORDER[level] <= _QUOTA_ORDER[entry.quota_level]:
        return False
    entry.quota_level = level
    _touch(entry)
    return True


def get_pending_interactive_ui(user_id: int, session_id: str) -> tuple[str, str] | None:
    """Return (content, ui_name) snapshot for a bg session in needs_action,
    or None if not in that state."""
    entry = _bg.get(user_id, {}).get(session_id)
    if entry is None:
        return None
    return entry.pending_interactive_ui


def mark_seen(user_id: int, session_id: str) -> None:
    """Flip ``seen=True`` if the status is finalised. Called when the user
    taps a session in the switcher. Working-state sessions are unaffected
    (they're still doing things — seen has no meaning yet)."""
    entry = _bg.get(user_id, {}).get(session_id)
    if entry is None:
        return
    if entry.status in ("finished", "error", "needs_action"):
        entry.seen = True


def prune_seen(user_id: int) -> bool:
    """Drop any seen+finalised entries from the user's panel. Called on
    switch-away. Returns True if anything was dropped."""
    bucket = _bg.get(user_id)
    if not bucket:
        return False
    dropped = [
        sid
        for sid, entry in bucket.items()
        if entry.seen and entry.status in ("finished", "error", "needs_action")
    ]
    for sid in dropped:
        bucket.pop(sid, None)
    if not bucket:
        _bg.pop(user_id, None)
    return bool(dropped)


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


def quota_glyph_for(user_id: int, session_id: str) -> str:
    """Return ``⚠️🟢/🟡/🔴`` for a session whose quota crossed a threshold,
    or empty string. Used by the active card header and by ``_badge`` for
    bg panel rows."""
    entry = _bg.get(user_id, {}).get(session_id)
    if entry is None:
        return ""
    return _QUOTA_EMOJI.get(entry.quota_level, "")


def threshold_to_quota_level(threshold: int) -> QuotaLevel:
    """Map an absolute token threshold to a quota level. ``threshold`` is
    the value returned by ``usage.pop_session_token_alert`` — i.e. the
    cumulative session-token bound that was just crossed."""
    g, y, r = config.bg_status_quota_thresholds
    if threshold >= r:
        return "red"
    if threshold >= y:
        return "yellow"
    if threshold >= g:
        return "green"
    return "none"


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


def _badge(sess: "Session", entry: BgStatus) -> str:
    """Render one panel row: [name] ⏳/✅/❌/❓ [⚠️🟢/🟡/🔴]."""
    name = sess.name or sess.id
    status_glyph = _STATUS_EMOJI.get(entry.status, "")
    quota_glyph = _QUOTA_EMOJI.get(entry.quota_level, "")
    parts = [f"[{name}]", status_glyph]
    if quota_glyph:
        parts.append(quota_glyph)
    return " ".join(p for p in parts if p)


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
                "quota_level": entry.quota_level,
                "seen": entry.seen,
                "last_change": entry.last_change,
            }
        if row:
            out[str(uid)] = row
    return out


def load_per_user(raw: dict[str, Any] | None) -> None:
    """Populate the in-memory map from a state.json blob. Skip malformed
    entries silently — restart should never be blocked by a corrupted
    panel snapshot."""
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
            quota_val = data.get("quota_level", "none")
            if quota_val not in ("none", "green", "yellow", "red"):
                quota_val = "none"
            try:
                last_change = float(data.get("last_change", 0.0))
            except (TypeError, ValueError):
                last_change = 0.0
            target[sid] = BgStatus(
                status=status_val,
                quota_level=quota_val,
                seen=bool(data.get("seen", False)),
                last_change=last_change,
            )
