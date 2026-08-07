"""Session-map and transcript-resolution behavior for SessionManager.

This mixin owns hook-written session-map reconciliation, window bindings,
and backend-specific transcript lookup. The public SessionManager remains in
ccbot.session; this module is an implementation leaf.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiofiles

from .config import config
from .session_keys import key_matches_window
from .session_models import ClaudeSession, Session, WindowState

logger = logging.getLogger("ccbot.session")


class SessionMapMixin:
    """Window/session-map operations mixed into SessionManager."""

    _session_map_lock: asyncio.Lock
    window_states: dict[str, WindowState]
    window_display_names: dict[str, str]
    sessions: dict[str, Session]
    agent_backend: str
    is_window_id: Any
    save_state: Any
    find_session_by_window: Any
    set_session_window: Any
    set_active_session: Any

    # --- session_map.json polling (hook-written window_id -> session) ---

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Accepts both canonical ``<source>:<wid>`` keys and grouped-
        session keys ``<source>-w<digits>:<wid>`` — older Claude hook
        builds wrote the latter when called from a client attached to a
        grouped session (see ``hook.py`` for the canonical fix).

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if config.session_map_file.exists():
                    async with aiofiles.open(config.session_map_file, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    if any(
                        info.get("session_id")
                        for k, info in session_map.items()
                        if key_matches_window(k, window_id)
                    ):
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
        """Serialize map reconciliation against bot-owned restore publication."""
        async with self._session_map_lock:
            await self._load_session_map_unlocked()

    async def _load_session_map_unlocked(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Accepts canonical (``<source>:<wid>``) and grouped-session
        (``<source>-w<digits>:<wid>``) keys — see ``key_matches_window``
        for why the latter exists. Cleans up window_states entries not
        present in the map. Updates window_display_names from the
        ``window_name`` field in values.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        valid_wids: set[str] = set()
        changed = False

        for key, info in session_map.items():
            # Extract window_id from any accepted key shape.
            window_id = ""
            if ":" in key:
                candidate = key.rsplit(":", 1)[1]
                if self.is_window_id(candidate) and key_matches_window(key, candidate):
                    window_id = candidate
            if not window_id:
                continue
            valid_wids.add(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            new_backend = info.get("backend", "claude")
            new_transcript_path = info.get("transcript_path", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_id)
            state.backend = (
                new_backend if new_backend in ("claude", "codex") else "claude"
            )
            if state.transcript_path != new_transcript_path:
                state.transcript_path = new_transcript_path
                changed = True
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
            if sess is not None:
                if sess.claude_session_id != new_sid:
                    sess.claude_session_id = new_sid
                    changed = True
                if sess.backend != state.backend:
                    sess.backend = state.backend
                    changed = True
                if not sess.workdir and new_cwd:
                    sess.workdir = new_cwd
                    changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # A fresh Codex window has no session_map entry until its first prompt
        # is accepted.  Keep provisional state for every bot Session still
        # bound to a window; deleting it here removed the transcript binding
        # and made first-turn delivery impossible to prove.
        bound_wids = {
            sess.window_id for sess in self.sessions.values() if sess.window_id
        }
        stale_wids = [
            w
            for w in self.window_states
            if w and w not in valid_wids and w not in bound_wids
        ]
        for wid in stale_wids:
            logger.info("Removing stale window_state: %s", wid)
            del self.window_states[wid]
            changed = True

        if changed:
            self.save_state()

    async def publish_codex_restore_binding(
        self,
        *,
        sess: Session,
        user_id: int,
        window_id: str,
        window_name: str,
        transcript_path: Path,
    ) -> None:
        """Publish a native Codex resume before exposing it as active.

        ``load_session_map`` used to delete the provisional WindowState before
        Codex emitted its first hook. The manager lock covers the complete
        file-publish + in-memory bind transaction, while the store's flock
        coordinates the file update with the external hook process.
        """
        if not transcript_path.is_file():
            raise RuntimeError(f"Codex rollout does not exist: {transcript_path}")
        from .session_map_store import upsert_session_map_entry

        key = f"{config.tmux_session_name}:{window_id}"
        entry = {
            "session_id": sess.claude_session_id,
            "cwd": sess.workdir,
            "window_name": window_name,
            "backend": "codex",
            "transcript_path": str(transcript_path),
        }
        async with self._session_map_lock:
            await asyncio.to_thread(
                upsert_session_map_entry,
                config.session_map_file,
                key,
                entry,
            )
            state = self.get_window_state(window_id)
            state.session_id = sess.claude_session_id
            state.cwd = sess.workdir
            state.window_name = window_name
            state.backend = "codex"
            state.transcript_path = str(transcript_path)
            self.set_session_window(sess.id, window_id)
            self.set_active_session(user_id, sess.id)

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

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """List existing sessions for the configured backend."""
        from . import codex_session_io, session_claude_io

        io = codex_session_io if self.agent_backend == "codex" else session_claude_io
        return await io.list_sessions_for_directory(cwd)

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a tmux window to the best matching Claude session.

        Uses persisted session_id + cwd; returns None if the file is gone
        and clears the stale window-state pointer when that happens.
        """
        from . import codex_session_io, session_claude_io

        state = self.get_window_state(window_id)
        if not state.session_id or not state.cwd:
            return None

        if state.backend == "codex":
            session = await codex_session_io.get_session_direct(
                state.session_id,
                state.cwd,
                state.transcript_path or None,
            )
        else:
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
