"""Dataclasses for the SessionManager: WindowState, ClaudeSession, Session.

Pulled out of ``session.py`` so the manager stays under the size budget
without losing the dataclasses' to_dict / from_dict round-trip logic to
the persistence module.

Public API:
  WindowState — per-tmux-window claude session id + cwd + display name.
  ClaudeSession — read-only view over a Claude transcript directory entry.
  Session — task-driven session record (the DM unit of UX).
  SessionState — the literal type alias used by Session.state.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal


SessionState = Literal["active", "idle", "archived", "completed", "lost"]


@dataclass
class WindowState:
    """Persistent state for a tmux window.

    Attributes:
        session_id: Associated Claude session ID (empty if not yet detected)
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
        )


@dataclass
class ClaudeSession:
    """Information about a Claude Code session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str
    token_total: int = 0  # input+output across all assistant turns


@dataclass
class Session:
    """A task-driven Claude Code session.

    The unit of UX in DM mode. One Session corresponds to one tmux window while
    in `active`/`idle` state; on archival the window is killed and the
    `claude_session_id` is kept so `claude --resume` can rehydrate on restore.

    Attributes:
        id: Short stable id (8 hex chars). Never changes for the lifetime of the record.
        name: Human-readable name (kebab-case). Auto-generated via Haiku, renameable.
        window_id: Current tmux window id (e.g. '@5'). Empty if archived.
        workdir: Working directory at session creation. Used as cwd for `claude --resume`.
        goal: Free-form goal description. Closed only by user via `/done`.
        state: 'active' | 'idle' | 'archived' | 'completed' | 'lost'.
        claude_session_id: Last known Claude session id (uuid). Set by SessionStart hook.
        created_at: Unix timestamp.
        last_event_at: Unix timestamp of most recent inbound or outbound activity.
        archived_at: Unix timestamp of archival, 0 while active.
        token_usage_total: Cumulative input+output tokens (parsed from JSONL).
        message_count: Cumulative claude turn count.
        was_lost: True if this session passed through the ``lost`` state
            before reaching its terminal state (archived/completed). Used
            to paint a "(lost)" tag in /archive so the user can tell that
            this session was never recovered from the tmux-vanish event.
    """

    id: str
    name: str
    window_id: str = ""
    workdir: str = ""
    goal: str = ""
    state: SessionState = "active"
    claude_session_id: str = ""
    created_at: float = 0.0
    last_event_at: float = 0.0
    archived_at: float = 0.0
    token_usage_total: int = 0
    message_count: int = 0
    was_lost: bool = False

    @staticmethod
    def new_id() -> str:
        """Generate a fresh short id (8 lowercase hex chars)."""
        return secrets.token_hex(4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "window_id": self.window_id,
            "workdir": self.workdir,
            "goal": self.goal,
            "state": self.state,
            "claude_session_id": self.claude_session_id,
            "created_at": self.created_at,
            "last_event_at": self.last_event_at,
            "archived_at": self.archived_at,
            "token_usage_total": self.token_usage_total,
            "message_count": self.message_count,
            "was_lost": self.was_lost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        state_val = data.get("state", "active")
        if state_val not in ("active", "idle", "archived", "completed", "lost"):
            state_val = "active"
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            window_id=data.get("window_id", ""),
            workdir=data.get("workdir", ""),
            goal=data.get("goal", ""),
            state=state_val,
            claude_session_id=data.get("claude_session_id", ""),
            created_at=float(data.get("created_at", 0.0)),
            last_event_at=float(data.get("last_event_at", 0.0)),
            archived_at=float(data.get("archived_at", 0.0)),
            token_usage_total=int(data.get("token_usage_total", 0)),
            message_count=int(data.get("message_count", 0)),
            was_lost=bool(data.get("was_lost", False)),
        )
