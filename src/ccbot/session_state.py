"""Persisted DM session-pool operations for SessionManager.

This mixin contains active-session routing, archive lifecycle, user settings,
summary caching, and Telegram carrier identifiers. The public class and
singleton continue to live in ccbot.session.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

from .config import config
from .session_node_state import NodeSessionStateMixin
from .session_settings_defaults import (
    DEFAULT_USER_SETTINGS as DEFAULT_SESSION_USER_SETTINGS,
)
from .session_models import reserve_owner, Session, SessionState

logger = logging.getLogger("ccbot.session")


class SessionStateMixin(NodeSessionStateMixin):
    """DM routing and persisted session-state operations."""

    user_window_offsets: dict[int, dict[str, int]]
    active_sessions: dict[int, str]
    active_sessions_by_node: dict[int, dict[str, str]]
    active_history: dict[int, list[str]]
    sessions: dict[str, Session]
    user_settings: dict[int, dict[str, Any]]
    summary_cache: dict[str, dict[str, Any]]
    last_switcher_msg_id: dict[int, int]
    card_msg_id: dict[int, int]
    agent_backend: str
    save_state: Any
    get_display_name: Any

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
        node_id = self.get_selected_node_id(user_id)
        # Prefer the compatibility pointer when present. Besides preserving
        # old state, this keeps direct state repairs made by recovery/tests
        # authoritative while the node-scoped map is being introduced.
        legacy_sid = self.active_sessions.get(user_id)
        legacy = self.sessions.get(legacy_sid) if legacy_sid else None
        if legacy is not None and legacy.node_id == node_id:
            sid = legacy_sid
        else:
            sid = self.active_sessions_by_node.get(user_id, {}).get(node_id)
        if not sid:
            return None
        session = self.sessions.get(sid)
        if session is None or session.state not in ("active", "idle"):
            return None
        return session

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
        session = self.sessions[session_id]
        prev = self.active_sessions.get(user_id)
        if prev and prev != session_id:
            history = self.active_history.setdefault(user_id, [])
            # Deduplicate — if prev is already in history, move it to top.
            if prev in history:
                history.remove(prev)
            history.append(prev)
            # Cap recent-history depth.
            if len(history) > 10:
                del history[: len(history) - 10]
        self.active_sessions_by_node.setdefault(user_id, {})[session.node_id] = (
            session_id
        )
        if self.get_selected_node_id(user_id) == session.node_id:
            self.active_sessions[user_id] = session_id
        else:
            self.active_sessions.pop(user_id, None)
        self.save_state()
        logger.info(
            "active_session_change user=%d prev=%s next=%s next_name=%s "
            "next_window=%s next_state=%s",
            user_id,
            prev or "-",
            session_id,
            session.name,
            session.window_id,
            session.state,
            extra={
                "event": "active_session_change",
                "user_id": user_id,
                "prev_session_id": prev,
                "next_session_id": session_id,
                "next_session_name": session.name,
                "next_window_id": session.window_id,
                "next_session_state": session.state,
            },
        )

    def list_user_sessions(
        self,
        user_id: int,
        *,
        states: tuple[SessionState, ...] = ("active", "idle"),
    ) -> list["Session"]:
        """List sessions for a user filtered by state. Active first, by name."""
        selected_node_id = self.get_selected_node_id(user_id)
        out = [
            s
            for s in self.sessions.values()
            if s.state in states and s.node_id == selected_node_id
        ]
        out.sort(key=lambda s: (s.state != "active", s.name or s.id))
        return out

    def get_session(self, session_id: str) -> "Session | None":
        return self.sessions.get(session_id)

    def find_session_by_window(self, window_id: str) -> "Session | None":
        for s in self.sessions.values():
            if s.window_id == window_id and s.state in ("active", "idle"):
                return s
        return None

    def create_session(
        self,
        *,
        name: str = "",
        window_id: str = "",
        workdir: str = "",
        goal: str = "",
        backend: str | None = None,
        default_reserve_user_id: int = 0,
        node_id: str = "local",
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
            backend=backend or self.agent_backend,
            default_reserve_user_id=default_reserve_user_id,
            node_id=node_id,
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

    def _replace_terminal_active_session(self, sess: Session) -> None:
        """Repair both active pointers after a live session becomes terminal."""
        user_ids = set(self.active_sessions) | set(self.active_sessions_by_node)
        for uid in user_ids:
            node_sessions = self.active_sessions_by_node.setdefault(uid, {})
            was_node_active = node_sessions.get(sess.node_id) == sess.id
            was_legacy_active = self.active_sessions.get(uid) == sess.id
            if was_node_active:
                node_sessions.pop(sess.node_id, None)
            if was_legacy_active:
                self.active_sessions.pop(uid, None)

            history = self.active_history.get(uid, [])
            while sess.id in history:
                history.remove(sess.id)
            if not (was_node_active or was_legacy_active):
                continue

            replacement: Session | None = None
            for candidate_id in reversed(history):
                candidate = self.sessions.get(candidate_id)
                if (
                    candidate is not None
                    and candidate.node_id == sess.node_id
                    and candidate.state in ("active", "idle")
                ):
                    replacement = candidate
                    break
            if replacement is None:
                continue
            while replacement.id in history:
                history.remove(replacement.id)
            node_sessions[sess.node_id] = replacement.id
            if self.get_selected_node_id(uid) == sess.node_id:
                self.active_sessions[uid] = replacement.id
            logger.info(
                "auto_active_replacement user=%d terminal=%s -> %s node=%s",
                uid,
                sess.id,
                replacement.id,
                sess.node_id,
                extra={
                    "event": "auto_active_replacement",
                    "user_id": uid,
                    "killed_session_id": sess.id,
                    "new_active_session_id": replacement.id,
                    "node_id": sess.node_id,
                },
            )

        for history in self.active_history.values():
            while sess.id in history:
                history.remove(sess.id)

    def mark_session_archived(
        self, session_id: str, *, completed: bool = False
    ) -> None:
        """Move a session to archived/completed state, drop window_id binding."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        if sess.state == "lost":
            # Carry the lost-marker into archival so /archive can tag it
            # explicitly (per user feedback on pivot #38). Without this
            # the row reads identical to a clean archive and the fact
            # that the tmux window died externally is lost forever.
            sess.was_lost = True
        sess.state = "completed" if completed else "archived"
        sess.archived_at = time.time()
        sess.window_id = ""
        sess.screenshot_file_id = ""
        sess.screenshot_pane_hash = ""
        sess.screenshot_cached_at = 0.0
        sess.screenshot_user_id = 0
        sess.screenshot_capture_kib = 0
        sess.screenshot_profile = ""
        # A request admitted for this exact session must never migrate to the
        # fallback active session after the target is closed.
        sess.pending_preprocessing.clear()
        self._replace_terminal_active_session(sess)
        # Drop any bg-status panel entry — an archived session shouldn't
        # linger as a stale ✅/❓ badge on the next user message.
        from .handlers import bg_status

        bg_status.clear_for_session(session_id)
        self.save_state()
        from . import metrics

        metrics.inc("sessions_completed" if completed else "sessions_archived")
        logger.info("Archived session %s (completed=%s)", session_id, completed)

    def mark_session_lost(self, session_id: str) -> None:
        """Mark a session as lost (its tmux window vanished externally)."""
        sess = self.sessions.get(session_id)
        if not sess:
            return
        sess.state = "lost"
        sess.window_id = ""
        sess.pending_preprocessing.clear()
        self._replace_terminal_active_session(sess)
        # Lost sessions can't make progress; remove from the bg panel.
        from .handlers import bg_status

        bg_status.clear_for_session(session_id)
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
            if reserve_owner(s):
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
        sess = self.sessions[session_id]
        del self.sessions[session_id]
        self._replace_terminal_active_session(sess)
        from .handlers import bg_status

        bg_status.clear_for_session(session_id)
        self.save_state()
        logger.info("Deleted session record %s", session_id)
        return True

    # --- User settings (set via the inline ⚙ menu) ---

    DEFAULT_USER_SETTINGS: ClassVar[dict[str, Any]] = dict(
        DEFAULT_SESSION_USER_SETTINGS
    )

    def get_user_settings(self, user_id: int) -> dict[str, Any]:
        """Return the user's settings, filling in defaults for missing keys."""
        stored = self.user_settings.get(user_id, {})
        merged: dict[str, Any] = dict(self.DEFAULT_USER_SETTINGS)
        merged.update(stored)
        # Backwards-compat: the old binary value "on" maps to the new
        # 3-state "auto". Read-side only; stored value lingers until the
        # user picks something on the settings screen.
        if merged.get("local_terminal") == "on":
            merged["local_terminal"] = "auto"
        # Zero used to disable coalescing. It is no longer offered because it
        # produces an edit for every event; migrate persisted zero to 2s.
        if merged.get("live_lag") == 0:
            merged["live_lag"] = 2
        try:
            page_lines = int(merged.get("card_page_lines", 30))
        except (TypeError, ValueError):
            page_lines = 30
        merged["card_page_lines"] = (
            page_lines if page_lines in (30, 50, 70, 100) else 30
        )
        legacy_spoiler_lines = stored.get("spoiler_block_lines", 10)
        if "spoiler_command_lines" not in stored:
            merged["spoiler_command_lines"] = legacy_spoiler_lines
        if "spoiler_result_lines" not in stored:
            merged["spoiler_result_lines"] = legacy_spoiler_lines
        for key in ("spoiler_command_lines", "spoiler_result_lines"):
            try:
                spoiler_lines = int(merged.get(key, 10))
            except (TypeError, ValueError):
                spoiler_lines = 10
            merged[key] = spoiler_lines if spoiler_lines in (5, 10, 30, 60) else 10
        # Before option visibility had its own key, manual/auto meant that
        # the user expected a Terminal action. Preserve that expectation but
        # never revive the removed automatic-launch behavior.
        if "option_button_terminal" not in stored:
            merged["option_button_terminal"] = stored.get("local_terminal") in (
                "on",
                "manual",
                "auto",
            )
        return merged

    def update_user_setting(self, user_id: int, key: str, value: Any) -> None:
        """Persist a single user setting."""
        if key not in self.DEFAULT_USER_SETTINGS:
            raise ValueError(f"Unknown setting key: {key}")
        bucket = self.user_settings.setdefault(user_id, {})
        bucket[key] = value
        self.save_state()

    def get_enabled_backends(self, user_id: int) -> tuple[str, ...]:
        """Return enabled backends, migrating old global selection on read."""
        raw = self.user_settings.get(user_id, {}).get("enabled_backends")
        values: list[str] = []
        if isinstance(raw, list):
            for value in raw:
                if value in ("claude", "codex") and value not in values:
                    values.append(value)
        if not values:
            values.append(self.agent_backend)
        return tuple(values)

    def get_default_backend(self, user_id: int) -> str:
        """Backend for the reserve and the one-backend new-session shortcut."""
        enabled = self.get_enabled_backends(user_id)
        selected = self.user_settings.get(user_id, {}).get("default_session_backend")
        return str(selected) if selected in enabled else enabled[0]

    def set_backend_enabled(self, user_id: int, backend: str, enabled: bool) -> None:
        """Enable/disable a backend without mutating already-created sessions."""
        if backend not in ("claude", "codex"):
            raise ValueError(f"Unknown agent backend: {backend}")
        values = list(self.get_enabled_backends(user_id))
        if enabled:
            if backend not in values:
                values.append(backend)
        elif backend in values:
            if len(values) == 1:
                raise RuntimeError("last backend cannot be disabled")
            values.remove(backend)
        bucket = self.user_settings.setdefault(user_id, {})
        bucket["enabled_backends"] = values
        if bucket.get("default_session_backend") not in values:
            bucket["default_session_backend"] = values[0]
        self.agent_backend = self.get_default_backend(user_id)
        config.agent_backend = self.agent_backend
        self.save_state()

    def set_default_backend(self, user_id: int, backend: str) -> None:
        """Choose which enabled backend owns the one prewarmed reserve."""
        if backend not in self.get_enabled_backends(user_id):
            raise ValueError("default backend must be enabled")
        self.user_settings.setdefault(user_id, {})["default_session_backend"] = backend
        self.agent_backend = backend
        config.agent_backend = backend
        self.save_state()

    def set_agent_backend(self, backend: str) -> None:
        """Persist the bot-wide backend used for every newly created session.

        Switching while a live session exists is rejected: a bot instance is
        deliberately single-backend at runtime. Archive/kill live sessions
        first; historical records retain their backend for safe inspection.
        """
        if backend not in ("claude", "codex"):
            raise ValueError(f"Unknown agent backend: {backend}")
        if backend == self.agent_backend:
            return
        live = [
            sess
            for sess in self.sessions.values()
            if sess.state in ("active", "idle") and sess.backend != backend
        ]
        if live:
            raise RuntimeError("archive live sessions before switching backend")
        self.agent_backend = backend
        config.agent_backend = backend
        self.save_state()
        logger.info("Bot-wide agent backend changed to %s", backend)

    # --- Summary cache (agent session id -> short readable summary) ---

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
        """Persist a generated summary for an agent session id."""
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
        """Re-attach a session to a (possibly new) tmux window after restore.

        A restored (or re-bound lost) session re-enters as if freshly created:
        ``created_at`` is bumped to now so the oldest -> newest switcher slots
        it at the far right rather than back in its original position.
        """
        sess = self.sessions.get(session_id)
        if not sess:
            return
        now = time.time()
        sess.window_id = window_id
        sess.state = "active"
        sess.created_at = now
        sess.last_event_at = now
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

    def get_card_msg(self, user_id: int) -> int | None:
        return self.card_msg_id.get(user_id)

    def set_card_msg(self, user_id: int, message_id: int, persist: bool = True) -> None:
        if self.card_msg_id.get(user_id) == message_id:
            return
        self.card_msg_id[user_id] = message_id
        # Persist eagerly so a restart can repaint the live card in place.
        # Atomic carrier hand-off persists this together with active_sessions.
        if persist:
            self.save_state()

    def clear_card_msg(self, user_id: int) -> None:
        if user_id in self.card_msg_id:
            del self.card_msg_id[user_id]
            self.save_state()

    # --- Reverse map: claude_session_id -> user(s) via active_sessions ---

    def all_user_sessions_with_claude_id(
        self, claude_session_id: str
    ) -> list[tuple[int, "Session"]]:
        """Return [(user_id, Session)] including non-active sessions for that claude id.

        Used to drive background-session live-card edits even when the session
        is not active for any user.

        The session pool is global (shared workspace), so a claude event is
        fanned out to **every** allowed user — each gets their own live card /
        panel in their own DM. With a single allowed user (the common case)
        this collapses to one (user_id, Session) per match, identical to the
        previous single-user behaviour. Users are sorted for deterministic
        ordering.
        """
        if not config.allowed_users:
            return []
        matched = [
            sess
            for sess in self.sessions.values()
            if sess.claude_session_id == claude_session_id
        ]
        out: list[tuple[int, "Session"]] = []
        for user_id in sorted(config.allowed_users):
            for sess in matched:
                out.append((user_id, sess))
        return out
