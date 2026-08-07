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
from .session_defaults import DEFAULT_IDLE_ARCHIVE_HOURS
from .session_models import Session, SessionState

logger = logging.getLogger("ccbot.session")


class SessionStateMixin:
    """DM routing and persisted session-state operations."""

    user_window_offsets: dict[int, dict[str, int]]
    active_sessions: dict[int, str]
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
        self.active_sessions[user_id] = session_id
        self.save_state()
        sess = self.sessions[session_id]
        logger.info(
            "active_session_change user=%d prev=%s next=%s next_name=%s "
            "next_window=%s next_state=%s",
            user_id,
            prev or "-",
            session_id,
            sess.name,
            sess.window_id,
            sess.state,
            extra={
                "event": "active_session_change",
                "user_id": user_id,
                "prev_session_id": prev,
                "next_session_id": session_id,
                "next_session_name": sess.name,
                "next_window_id": sess.window_id,
                "next_session_state": sess.state,
            },
        )

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

    def create_session(
        self,
        *,
        name: str = "",
        window_id: str = "",
        workdir: str = "",
        goal: str = "",
        backend: str | None = None,
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
        if sess.state == "lost":
            # Carry the lost-marker into archival so /archive can tag it
            # explicitly (per user feedback on pivot #38). Without this
            # the row reads identical to a clean archive and the fact
            # that the tmux window died externally is lost forever.
            sess.was_lost = True
        sess.state = "completed" if completed else "archived"
        sess.archived_at = time.time()
        sess.window_id = ""
        # If this was anyone's active session, auto-pick the
        # previously-active session as the replacement (per user
        # request: "при удалении активной сессии необходимо
        # автоматически выбирать последнюю активную до нее"). Walks
        # ``active_history`` newest-first, skipping any entries that
        # are themselves no longer live.
        for uid, sid in list(self.active_sessions.items()):
            if sid != session_id:
                continue
            del self.active_sessions[uid]
            history = self.active_history.get(uid, [])
            # Also drop the just-archived session from history if
            # present so it can't be re-picked later.
            while session_id in history:
                history.remove(session_id)
            while history:
                candidate_id = history.pop()
                candidate = self.sessions.get(candidate_id)
                if candidate is not None and candidate.state in (
                    "active",
                    "idle",
                ):
                    self.active_sessions[uid] = candidate_id
                    logger.info(
                        "auto_active_replacement user=%d killed=%s -> %s",
                        uid,
                        session_id,
                        candidate_id,
                        extra={
                            "event": "auto_active_replacement",
                            "user_id": uid,
                            "killed_session_id": session_id,
                            "new_active_session_id": candidate_id,
                        },
                    )
                    break
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
        # Defensive auto-replacement: delete is normally called on already-
        # archived sessions, but if a record is purged while still listed as
        # active, walk active_history newest-first to pick a successor (same
        # rule as ``mark_session_archived``).
        for uid, sid in list(self.active_sessions.items()):
            if sid != session_id:
                continue
            del self.active_sessions[uid]
            history = self.active_history.get(uid, [])
            while history:
                candidate_id = history.pop()
                candidate = self.sessions.get(candidate_id)
                if candidate is not None and candidate.state in ("active", "idle"):
                    self.active_sessions[uid] = candidate_id
                    break
        for hist in self.active_history.values():
            while session_id in hist:
                hist.remove(session_id)
        from .handlers import bg_status

        bg_status.clear_for_session(session_id)
        self.save_state()
        logger.info("Deleted session record %s", session_id)
        return True

    # --- User settings (set via the inline ⚙ menu) ---

    DEFAULT_USER_SETTINGS: ClassVar[dict[str, Any]] = {
        "language": "en",  # "en" | "ru" | "zh" — UI strings
        "live_lag": 4,  # seconds, see PREVIEW_LIVE_LAG
        "voice": "auto",  # "auto" | "whisper" | "apple" | "off"
        # Hours without activity before a live session is archived. 6h is the
        # closest supported migration from the historical global 4h default.
        "session_idle_hours": DEFAULT_IDLE_ARCHIVE_HOURS,
        # Day-of-week the Anthropic weekly window resets on. Drives the %/d
        # burn-rate computation in Menu → Status. Values: "mon".."sun".
        "weekly_reset_day": "mon",
        # Auto-approve interactive Yes/No prompts that --dangerously-skip-
        # permissions doesn't already bypass (e.g. WebFetch per-domain
        # trust). "off" = surface in TG, "on" = auto-Yes on every prompt.
        "auto_approve": "off",
        # Three states for the desktop terminal companion:
        #   off    — never spawn, never offer
        #   manual — don't auto-spawn, but show "Open terminal" in Menu
        #            when the active session has no attached tmux client
        #   auto   — auto-spawn on session create AND show the manual
        #            button whenever no client is attached
        # On Linux ``manual``/``auto`` also need ``local_terminal_cmd``
        # (or CCBOT_LOCAL_TERMINAL_CMD env) — without an emulator template
        # the button is hidden because the click would silently no-op.
        # Legacy binary "on" is auto-migrated to "auto" on read.
        "local_terminal": "off",
        # Linux: command template used by ``local_terminal``. Empty means
        # "fall back to CCBOT_LOCAL_TERMINAL_CMD or skip". Templates are
        # picked from a known list in Settings → Local terminal, or set
        # manually via env. Use ``{shell}`` as the placeholder for the
        # shell-quoted attach snippet.
        "local_terminal_cmd": "",
        # Disposition of the user's outgoing text relative to the live
        # How many trailing end_turn boundaries to pull from the JSONL
        # transcript when seeding an empty live-card state (e.g. after
        # a bot restart, after switcher-tap / Menu → Sessions on a fresh
        # state). Higher = more in-card scrollback at the cost of memory
        # (each turn ≈ several events × ~500 bytes). Deep history is
        # always accessible via /history regardless of this setting.
        "card_history": 20,
        # Inline screenshots — the pane render is the last media block of
        # the active Rich Markdown card instead of a separate Shot photo.
        # Updates are throttled to one media edit per ~3 sec and skipped
        # when the pane is unchanged. Older Bot API servers fall back to
        # the legacy photo+caption transport.
        "card_inline_screenshots": False,
        # Bg session push notifications (Task #42). Three independent
        # toggles — user asked to make each granular. Default all-on
        # so the user knows what bg sessions are doing.
        "bg_notify_finished": True,
        "bg_notify_error": True,
        "bg_notify_needs_action": True,
        # Max page size in logical \n-delimited LINES. Values 10/20/40/70.
        # 20 keeps the card compact on phone; 70 is for power users who
        # scroll long bodies. Anchor (page top) chunking handles overflow
        # with smart sentence / paragraph boundaries — see
        # ``_chunk_final_text`` for the exact preference order.
        "card_page_lines": 20,
        # Auto-rename new sessions via a cheap one-shot model call after the
        # first user message ≥20 chars. When ``False``, names stay as
        # the directory basename (``workdir``, ``workdir-2``, ...) for
        # the session's lifetime. The persisted key keeps its historical
        # name for state-file compatibility.
        "haiku_naming": True,
    }

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
        return merged

    def update_user_setting(self, user_id: int, key: str, value: Any) -> None:
        """Persist a single user setting."""
        if key not in self.DEFAULT_USER_SETTINGS:
            raise ValueError(f"Unknown setting key: {key}")
        bucket = self.user_settings.setdefault(user_id, {})
        bucket[key] = value
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

    def set_card_msg(self, user_id: int, message_id: int) -> None:
        if self.card_msg_id.get(user_id) == message_id:
            return
        self.card_msg_id[user_id] = message_id
        # Persist eagerly so a restart can repaint the live card in place.
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
