"""Claude Code session management — the core state hub.

Manages the key mappings (DM mode):
  user_id -> active_session: which Session is currently active for a user.
  short id -> Session: full per-session metadata (goal, window, state, timestamps, usage).
  window_id -> WindowState: which Claude session_id a tmux window holds.

Responsibilities:
  - Persist/load state to ~/.ccbot/state.json.
  - Sync window<->session bindings from session_map.json (written by hook).
  - Resolve window IDs to ClaudeSession objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage active_sessions: lookup, switch, create, archive, restore.
  - Send keystrokes to tmux windows and retrieve message history.
  - Maintain window_id<->display name mapping for UI display.
  - Re-resolve stale window IDs on startup (tmux server restart recovery).

Key classes:
  SessionManager (singleton `session_manager`).
  Session — per-task record with goal, lifecycle state, timestamps.
  WindowState — per-tmux-window claude_session_id + cwd.
  ClaudeSession — read-only Claude transcript metadata.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import aiofiles

from .config import config
from .session_models import ClaudeSession, Session, SessionState, WindowState
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

# Re-export for callers that still import these names from `ccbot.session`.
__all__ = [
    "ClaudeSession",
    "Session",
    "SessionState",
    "SessionManager",
    "WindowState",
    "session_manager",
]

logger = logging.getLogger(__name__)


@dataclass
class SessionManager:
    """Manages session state for Claude Code.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    window_display_names: window_id -> window_name (for display)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    # DM mode: routing key for inbound user text.
    # user_id -> Session.id (short hex). Single active session per user.
    active_sessions: dict[int, str] = field(default_factory=dict)
    # All sessions known to the bot (active, idle, archived, completed, lost).
    # Keyed by Session.id.
    sessions: dict[str, "Session"] = field(default_factory=dict)
    # Telegram message_id of the bot message that currently carries the inline
    # session switcher for each user. Used to strip stale switchers when a new
    # bot message goes out.
    last_switcher_msg_id: dict[int, int] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # User-scoped UI/runtime preferences (set via the inline ⚙ menu).
    # user_id -> {key: value}. Defaults are filled by get_user_settings.
    user_settings: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Cached short summaries for ClaudeSession picker. Key = claude session id.
    # Value = {"summary": str, "mtime": float, "ts": float}.
    summary_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def save_state(self) -> None:
        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "active_sessions": {
                str(uid): sid for uid, sid in self.active_sessions.items()
            },
            "sessions": {sid: s.to_dict() for sid, s in self.sessions.items()},
            "last_switcher_msg_id": {
                str(uid): mid for uid, mid in self.last_switcher_msg_id.items()
            },
            "window_display_names": self.window_display_names,
            "user_settings": {
                str(uid): vals for uid, vals in self.user_settings.items()
            },
            "summary_cache": self.summary_cache,
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.active_sessions = {
                    int(uid): sid
                    for uid, sid in state.get("active_sessions", {}).items()
                }
                self.sessions = {
                    sid: Session.from_dict(data)
                    for sid, data in state.get("sessions", {}).items()
                }
                self.last_switcher_msg_id = {
                    int(uid): int(mid)
                    for uid, mid in state.get("last_switcher_msg_id", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.user_settings = {
                    int(uid): dict(vals)
                    for uid, vals in state.get("user_settings", {}).items()
                }
                self.summary_cache = dict(state.get("summary_cache", {}))

                # Detect old format: window_states keys that don't look like
                # tmux window IDs ("@N"). resolve_stale_ids re-maps on startup.
                needs_migration = any(
                    not self.is_window_id(k) for k in self.window_states
                )
                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.active_sessions = {}
                self.sessions = {}
                self.last_switcher_msg_id = {}
                self.window_display_names = {}
                self.user_settings = {}
                self.summary_cache = {}

    async def reconcile_sessions_with_tmux(self) -> int:
        """Mark sessions whose tmux window vanished as ``lost``."""
        from . import session_recovery

        return await session_recovery.reconcile_with_tmux(self)

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows."""
        from . import session_recovery

        await session_recovery.resolve_stale_window_ids(self)

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update WindowState.window_name if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self.save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        key = f"{config.tmux_session_name}:{window_id}"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if config.session_map_file.exists():
                    async with aiofiles.open(config.session_map_file, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    info = session_map.get(key, {})
                    if info.get("session_id"):
                        # Found — load into window_states immediately
                        logger.debug(
                            "session_map entry found for window_id %s", window_id
                        )
                        await self.load_session_map()
                        return True
            except (json.JSONDecodeError, OSError):
                pass
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Keys in session_map are formatted as "tmux_session:window_id" (e.g. "ccbot:@12").
        Only entries matching our tmux_session_name are processed.
        Also cleans up window_states entries not in current session_map.
        Updates window_display_names from the "window_name" field in values.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = f"{config.tmux_session_name}:"
        valid_wids: set[str] = set()
        changed = False

        for key, info in session_map.items():
            # Only process entries for our tmux session
            if not key.startswith(prefix):
                continue
            window_id = key[len(prefix) :]
            if not self.is_window_id(window_id):
                continue
            valid_wids.add(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_id)
            if state.session_id != new_sid or state.cwd != new_cwd:
                logger.info(
                    "Session map: window_id %s updated sid=%s, cwd=%s",
                    window_id,
                    new_sid,
                    new_cwd,
                )
                state.session_id = new_sid
                state.cwd = new_cwd
                changed = True
            # Mirror the claude session id onto any Session record bound to this window.
            sess = self.find_session_by_window(window_id)
            if sess is not None and sess.claude_session_id != new_sid:
                sess.claude_session_id = new_sid
                if not sess.workdir and new_cwd:
                    sess.workdir = new_cwd
                changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # Clean up window_states entries not in current session_map.
        stale_wids = [w for w in self.window_states if w and w not in valid_wids]
        for wid in stale_wids:
            logger.info("Removing stale window_state: %s", wid)
            del self.window_states[wid]
            changed = True

        if changed:
            self.save_state()

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        self.save_state()
        logger.info("Cleared session for window_id %s", window_id)

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Backwards-compatible re-export of ``session_claude_io.encode_cwd``."""
        from . import session_claude_io

        return session_claude_io.encode_cwd(cwd)

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """List existing Claude sessions for a directory (newest first, max 10)."""
        from . import session_claude_io

        return await session_claude_io.list_sessions_for_directory(cwd)

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a tmux window to the best matching Claude session.

        Uses persisted session_id + cwd; returns None if the file is gone
        and clears the stale window-state pointer when that happens.
        """
        from . import session_claude_io

        state = self.get_window_state(window_id)
        if not state.session_id or not state.cwd:
            return None

        session = await session_claude_io.get_session_direct(
            state.session_id, state.cwd
        )
        if session:
            return session

        logger.warning(
            "Session file no longer exists for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        state.session_id = ""
        state.cwd = ""
        self.save_state()
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self.save_state()

    # --- DM mode: active session management ---

    def get_active_session(self, user_id: int) -> "Session | None":
        """Return the currently active Session for a user, or None."""
        sid = self.active_sessions.get(user_id)
        if not sid:
            return None
        return self.sessions.get(sid)

    def get_active_window(self, user_id: int) -> str | None:
        """Return the tmux window_id of the user's active session, or None."""
        sess = self.get_active_session(user_id)
        if sess is None or not sess.window_id or sess.state not in ("active", "idle"):
            return None
        return sess.window_id

    def set_active_session(self, user_id: int, session_id: str) -> None:
        """Make `session_id` the active session for `user_id`."""
        if session_id not in self.sessions:
            raise KeyError(f"Unknown session id: {session_id}")
        self.active_sessions[user_id] = session_id
        self.save_state()
        logger.info("Active session for user %d: %s", user_id, session_id)

    def clear_active_session(self, user_id: int) -> None:
        """Drop the active-session pointer for a user (e.g. all sessions archived)."""
        if user_id in self.active_sessions:
            del self.active_sessions[user_id]
            self.save_state()

    def list_user_sessions(
        self,
        user_id: int,
        *,
        states: tuple[SessionState, ...] = ("active", "idle"),
    ) -> list["Session"]:
        """List sessions for a user filtered by state. Active first, by name."""
        # In v0.1 every session is implicitly the bot's single user's; we still
        # accept user_id so the public surface is uniform with other helpers.
        del user_id  # no per-user partitioning yet
        out = [s for s in self.sessions.values() if s.state in states]
        out.sort(key=lambda s: (s.state != "active", s.name or s.id))
        return out

    def get_session(self, session_id: str) -> "Session | None":
        return self.sessions.get(session_id)

    def find_session_by_window(self, window_id: str) -> "Session | None":
        for s in self.sessions.values():
            if s.window_id == window_id and s.state in ("active", "idle"):
                return s
        return None

    def find_session_by_claude_id(self, claude_session_id: str) -> "Session | None":
        for s in self.sessions.values():
            if s.claude_session_id == claude_session_id:
                return s
        return None

    def create_session(
        self,
        *,
        name: str = "",
        window_id: str = "",
        workdir: str = "",
        goal: str = "",
    ) -> "Session":
        """Register a new Session record. Caller is responsible for the tmux window."""
        now = time.time()
        sid = Session.new_id()
        # Avoid id collision in pathological case
        while sid in self.sessions:
            sid = Session.new_id()
        if not name:
            name = f"session-{len(self.sessions) + 1}"
        sess = Session(
            id=sid,
            name=name,
            window_id=window_id,
            workdir=workdir,
            goal=goal,
            state="active",
            created_at=now,
            last_event_at=now,
        )
        self.sessions[sid] = sess
        self.save_state()
        from . import metrics

        metrics.inc("sessions_created")
        logger.info("Created session %s (%s) on window %s", sid, name, window_id or "-")
        return sess

    def touch_session(self, session_id: str) -> None:
        """Bump last_event_at to now and persist."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.last_event_at = time.time()
        # Don't save on every touch; callers batch via _save_state when appropriate.

    def mark_session_archived(
        self, session_id: str, *, completed: bool = False
    ) -> None:
        """Move a session to archived/completed state, drop window_id binding."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.state = "completed" if completed else "archived"
        sess.archived_at = time.time()
        sess.window_id = ""
        # If this was anyone's active session, clear that
        for uid, sid in list(self.active_sessions.items()):
            if sid == session_id:
                del self.active_sessions[uid]
        self.save_state()
        from . import metrics

        metrics.inc(
            "sessions_completed" if completed else "sessions_archived"
        )
        logger.info("Archived session %s (completed=%s)", session_id, completed)

    def mark_session_lost(self, session_id: str) -> None:
        """Mark a session as lost (its tmux window vanished externally)."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.state = "lost"
        sess.window_id = ""
        self.save_state()
        logger.warning("Session %s marked lost", session_id)

    def list_archived(
        self,
        *,
        max_age_seconds: float | None = None,
        states: tuple[SessionState, ...] = ("archived", "completed", "lost"),
    ) -> list["Session"]:
        """Return archived/completed/lost sessions, newest first.

        If `max_age_seconds` is given, only sessions whose archived_at is
        within that window are returned.
        """
        now = time.time()
        out: list[Session] = []
        for s in self.sessions.values():
            if s.state not in states:
                continue
            if max_age_seconds is not None:
                # Use archived_at if set, else last_event_at as fallback.
                anchor = s.archived_at or s.last_event_at or s.created_at
                if anchor and (now - anchor) > max_age_seconds:
                    continue
            out.append(s)
        out.sort(key=lambda s: s.archived_at or s.last_event_at or 0, reverse=True)
        return out

    def find_idle_to_archive(self, idle_seconds: float) -> list["Session"]:
        """Return active/idle sessions that have crossed the idle TTL threshold."""
        if idle_seconds <= 0:
            return []
        now = time.time()
        out: list[Session] = []
        for s in self.sessions.values():
            if s.state not in ("active", "idle"):
                continue
            anchor = s.last_event_at or s.created_at
            if anchor and (now - anchor) >= idle_seconds:
                out.append(s)
        return out

    def find_archive_to_purge(self, purge_after_seconds: float) -> list["Session"]:
        """Return archived/completed/lost sessions older than the purge threshold."""
        if purge_after_seconds <= 0:
            return []
        now = time.time()
        out: list[Session] = []
        for s in self.sessions.values():
            if s.state not in ("archived", "completed", "lost"):
                continue
            anchor = s.archived_at or s.last_event_at or s.created_at
            if anchor and (now - anchor) >= purge_after_seconds:
                out.append(s)
        return out

    def delete_session(self, session_id: str) -> bool:
        """Permanently remove a Session record. Transcripts on disk are kept."""
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        for uid, sid in list(self.active_sessions.items()):
            if sid == session_id:
                del self.active_sessions[uid]
        self.save_state()
        logger.info("Deleted session record %s", session_id)
        return True

    # --- User settings (set via the inline ⚙ menu) ---

    DEFAULT_USER_SETTINGS: ClassVar[dict[str, Any]] = {
        "language": "en",  # "en" | "ru" | "zh" — UI strings
        "previews": "economical",  # "economical" | "readable" (Haiku-cached)
        "live_lag": 4,  # seconds, see PREVIEW_LIVE_LAG
        "voice": "auto",  # "auto" | "whisper" | "apple" | "off"
        # Day-of-week the Anthropic weekly window resets on. Drives the %/d
        # burn-rate computation in /status. Values: "mon".."sun".
        "weekly_reset_day": "mon",
        # Auto-approve interactive Yes/No prompts that --dangerously-skip-
        # permissions doesn't already bypass (e.g. WebFetch per-domain
        # trust). "off" = surface in TG, "on" = auto-Yes on every prompt.
        "auto_approve": "off",
        # Three ascending per-session token thresholds (in tokens). Each
        # triggers a one-shot push notification when the session crosses it.
        # Adjustable in 50_000-token steps via Settings.
        "session_token_alerts": [100_000, 200_000, 400_000],
        # macOS UX: when "on", every newly-created session also pops a
        # native Terminal/iTerm window attached to its tmux window, so the
        # user can drive the session by hand from the desktop in parallel
        # with the Telegram UI. No-op on non-macOS hosts.
        "local_terminal": "off",
    }

    def get_user_settings(self, user_id: int) -> dict[str, Any]:
        """Return the user's settings, filling in defaults for missing keys."""
        stored = self.user_settings.get(user_id, {})
        merged: dict[str, Any] = dict(self.DEFAULT_USER_SETTINGS)
        merged.update(stored)
        return merged

    def update_user_setting(self, user_id: int, key: str, value: Any) -> None:
        """Persist a single user setting."""
        if key not in self.DEFAULT_USER_SETTINGS:
            raise ValueError(f"Unknown setting key: {key}")
        bucket = self.user_settings.setdefault(user_id, {})
        bucket[key] = value
        self.save_state()

    # --- Summary cache (Claude session id -> short readable summary) ---

    def get_cached_summary(
        self, claude_session_id: str, file_mtime: float
    ) -> str | None:
        """Return cached summary if mtime matches; otherwise None."""
        entry = self.summary_cache.get(claude_session_id)
        if not entry:
            return None
        if abs(float(entry.get("mtime", 0.0)) - file_mtime) > 1e-3:
            return None
        return entry.get("summary") or None

    def set_cached_summary(
        self, claude_session_id: str, summary: str, file_mtime: float
    ) -> None:
        """Persist a generated summary for a Claude session id."""
        if not claude_session_id or not summary:
            return
        self.summary_cache[claude_session_id] = {
            "summary": summary,
            "mtime": file_mtime,
            "ts": time.time(),
        }
        self.save_state()

    def rename_session(self, session_id: str, new_name: str) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.name = new_name
        self.save_state()

    def set_session_window(self, session_id: str, window_id: str) -> None:
        """Re-attach a session to a (possibly new) tmux window after restore."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.window_id = window_id
        sess.state = "active"
        sess.last_event_at = time.time()
        self.save_state()

    def set_session_goal(self, session_id: str, goal: str) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.goal = goal
        self.save_state()

    def set_session_claude_id(self, session_id: str, claude_session_id: str) -> None:
        sess = self.sessions.get(session_id)
        if not sess:
            return
        if sess.claude_session_id != claude_session_id:
            sess.claude_session_id = claude_session_id
            self.save_state()

    def get_last_switcher_msg(self, user_id: int) -> int | None:
        return self.last_switcher_msg_id.get(user_id)

    def set_last_switcher_msg(self, user_id: int, message_id: int) -> None:
        self.last_switcher_msg_id[user_id] = message_id
        # Persist eagerly: cheap, helps survive bot restart for switcher cleanup.
        self.save_state()

    def clear_last_switcher_msg(self, user_id: int) -> None:
        if user_id in self.last_switcher_msg_id:
            del self.last_switcher_msg_id[user_id]
            self.save_state()

    # --- Legacy thread binding management (used during migration; removed in Phase 1) ---

    async def find_users_for_claude_session(
        self,
        claude_session_id: str,
    ) -> list[tuple[int, "Session"]]:
        """Return [(user_id, Session)] for every user whose active session matches.

        Reverse-map of (user_id -> active Session) by claude_session_id.
        Background sessions of a user are NOT returned here — outbound for those
        flows through their own per-session live cards (see C7).
        """
        out: list[tuple[int, "Session"]] = []
        for user_id, sid in self.active_sessions.items():
            sess = self.sessions.get(sid)
            if sess and sess.claude_session_id == claude_session_id:
                out.append((user_id, sess))
        return out

    def all_user_sessions_with_claude_id(
        self, claude_session_id: str
    ) -> list[tuple[int, "Session"]]:
        """Return [(user_id, Session)] including non-active sessions for that claude id.

        Used to drive background-session live-card edits even when the session
        is not active for any user. The user_id is best-effort and currently
        always equals the bot's single allowed user (DM mode).
        """
        if not config.allowed_users:
            return []
        # In v0.1 we model a single user; pick the deterministic minimum.
        user_id = min(config.allowed_users)
        out: list[tuple[int, "Session"]] = []
        for sess in self.sessions.values():
            if sess.claude_session_id == claude_session_id:
                out.append((user_id, sess))
        return out

    # --- Tmux helpers ---

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a tmux window by ID."""
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        success = await tmux_manager.send_keys(window.window_id, text)
        if success:
            return True, f"Sent to {display}"
        return False, "Failed to send keys"

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict[str, Any]] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
