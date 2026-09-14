"""Monitor state persistence — tracks byte offsets for each session.

Persists TrackedSession records (session_id, file_path, last_byte_offset)
to ~/.ccbot/monitor_state.json so the session monitor can resume
incremental reading after restarts without re-sending old messages.

Key classes: MonitorState, TrackedSession.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MONITOR_CHECKPOINT_INTERVAL = 30.0


@dataclass
class TrackedSession:
    """State for a tracked Claude Code session."""

    session_id: str
    file_path: str  # Path to .jsonl file
    last_byte_offset: int = 0  # Byte offset for incremental reading

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedSession":
        """Create from dict."""
        return cls(
            session_id=data.get("session_id", ""),
            file_path=data.get("file_path", ""),
            last_byte_offset=data.get("last_byte_offset", 0),
        )


@dataclass
class MonitorState:
    """Persistent state for the session monitor.

    Stores tracking information for all monitored sessions
    to prevent duplicate notifications after restarts.
    """

    state_file: Path
    checkpoint_interval: float = MONITOR_CHECKPOINT_INTERVAL
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    tracked_sessions: dict[str, TrackedSession] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)
    _urgent: bool = field(default=False, repr=False)
    _last_save_at: float = field(default=0.0, init=False, repr=False)
    _committed_sessions: dict[str, TrackedSession] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._last_save_at = self.clock()

    @staticmethod
    def _clone_sessions(
        sessions: dict[str, TrackedSession],
    ) -> dict[str, TrackedSession]:
        return {
            session_id: TrackedSession.from_dict(session.to_dict())
            for session_id, session in sessions.items()
        }

    def load(self) -> None:
        """Load state from file."""
        if not self.state_file.exists():
            logger.debug(f"State file does not exist: {self.state_file}")
            return

        try:
            data = json.loads(self.state_file.read_text())
            sessions = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                k: TrackedSession.from_dict(v) for k, v in sessions.items()
            }
            self._committed_sessions = self._clone_sessions(self.tracked_sessions)
            self._dirty = False
            self._urgent = False
            self._last_save_at = self.clock()
            logger.info(
                f"Loaded {len(self.tracked_sessions)} tracked sessions from state"
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to load state file: {e}")
            self.tracked_sessions = {}
            self._committed_sessions = {}
            self._dirty = False
            self._urgent = False

    def save(self) -> None:
        """Save state to file atomically."""
        from .utils import atomic_write_json

        data = {
            "tracked_sessions": {
                k: v.to_dict() for k, v in self.tracked_sessions.items()
            }
        }

        try:
            atomic_write_json(self.state_file, data)
            self._dirty = False
            self._urgent = False
            self._last_save_at = self.clock()
            self._committed_sessions = self._clone_sessions(self.tracked_sessions)
            logger.debug(
                "Saved %d tracked sessions to state", len(self.tracked_sessions)
            )
        except OSError as e:
            logger.error("Failed to save state file: %s", e)

    def get_session(self, session_id: str) -> TrackedSession | None:
        """Get tracked session by ID."""
        return self.tracked_sessions.get(session_id)

    def update_session(self, session: TrackedSession) -> None:
        """Update or add a tracked session."""
        is_new = session.session_id not in self.tracked_sessions
        self.tracked_sessions[session.session_id] = session
        self._dirty = True
        if is_new:
            # Persist the initial EOF immediately. Deferring a newly tracked
            # session would let a quick restart skip output written between
            # first discovery and the restart.
            self._urgent = True

    def remove_session(self, session_id: str) -> None:
        """Remove a tracked session."""
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True
            self._urgent = True

    def save_if_dirty(self) -> None:
        """Save state only if it has been modified."""
        if self._dirty:
            self.save()

    def save_if_due(self, *, force: bool = False) -> None:
        """Checkpoint dirty offsets, coalescing ordinary streaming updates.

        New/removed sessions and explicit ``force`` calls are persisted
        immediately. Existing-session offsets may remain in memory for at
        most ``checkpoint_interval`` seconds; this removes one fsync per
        transcript poll without delaying message delivery.
        """
        if not self._dirty:
            return
        if (
            force
            or self._urgent
            or self.clock() - self._last_save_at >= self.checkpoint_interval
        ):
            self.save()

    def restore_committed(self, session_ids: set[str] | None = None) -> None:
        """Restore durable offsets for all or selected failed sessions."""
        if session_ids is None:
            self.tracked_sessions = self._clone_sessions(self._committed_sessions)
        else:
            for session_id in session_ids:
                committed = self._committed_sessions.get(session_id)
                if committed is None:
                    self.tracked_sessions.pop(session_id, None)
                else:
                    self.tracked_sessions[session_id] = TrackedSession.from_dict(
                        committed.to_dict()
                    )
        self._dirty = self.tracked_sessions != self._committed_sessions
        self._urgent = self.tracked_sessions.keys() != self._committed_sessions.keys()
