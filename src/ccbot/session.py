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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles

if TYPE_CHECKING:
    from telegram import Bot

from .config import config
from .session_defaults import DEFAULT_IDLE_ARCHIVE_HOURS, IDLE_ARCHIVE_HOUR_CHOICES
from .session_keys import key_matches_window
from .session_map import SessionMapMixin
from .session_models import ClaudeSession, Session, SessionState, WindowState
from .session_state import SessionStateMixin
from .terminal_parser import is_interactive_ui, parse_status_line
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

# Re-export for callers that still import these names from `ccbot.session`.
__all__ = [
    "ClaudeSession",
    "DEFAULT_IDLE_ARCHIVE_HOURS",
    "IDLE_ARCHIVE_HOUR_CHOICES",
    "Session",
    "SessionState",
    "SessionManager",
    "WindowState",
    "key_matches_window",
    "session_manager",
]

logger = logging.getLogger(__name__)

# Resume-settle gate tuning (see SessionManager._wait_for_resume_settle).
_RESUME_SETTLE_BUSY_GRACE = 6.0  # s to wait for a compaction spinner to appear
_RESUME_SETTLE_IDLE_STABLE = 4.0  # s the pane must stay idle to count as settled
_RESUME_SETTLE_POLL = 1.5  # s between pane captures
# Inter-send gap when the background watcher drains buffered prompts —
# claude's TUI needs a tick to separate back-to-back Enter submissions.
_RESUME_SETTLE_DRAIN_GAP = 0.3
# How often the watcher fires Telegram TYPING while waiting. ~4s matches
# fire_typing's own throttle and Telegram's ~5s indicator decay.
_RESUME_SETTLE_TYPING_REFRESH = 4.0


@dataclass
class SessionManager(SessionMapMixin, SessionStateMixin):
    """Manages session state for Claude Code.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    window_display_names: window_id -> window_name (for display)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    _session_map_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    # DM mode: routing key for inbound user text.
    # user_id -> Session.id (short hex). Single active session per user.
    active_sessions: dict[int, str] = field(default_factory=dict)
    # Stack of previously-active session ids per user (most recent at the
    # end). Used by ``mark_session_archived`` to auto-pick the next
    # active session when the current one gets killed — without this the
    # user ends up with no active session and the live card shows the
    # empty state. Capped at the last 10 entries per user.
    active_history: dict[int, list[str]] = field(default_factory=dict)
    # All sessions known to the bot (active, idle, archived, completed, lost).
    # Keyed by Session.id.
    sessions: dict[str, "Session"] = field(default_factory=dict)
    # Telegram message_id of the bot message that currently carries the inline
    # session switcher for each user. Used to strip stale switchers when a new
    # bot message goes out.
    last_switcher_msg_id: dict[int, int] = field(default_factory=dict)
    # Telegram message_id of the bot message hosting each user's live card
    # for their *active* session. Persisted so a bot restart can repaint
    # the card in place (notifications.restore_card) instead of orphaning
    # it and starting fresh on the next event. user_id -> message_id.
    card_msg_id: dict[int, int] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # User-scoped UI/runtime preferences (set via the inline ⚙ menu).
    # user_id -> {key: value}. Defaults are filled by get_user_settings.
    user_settings: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Bot-wide agent mode selected from Telegram Settings. The env value is
    # only the initial default for a state file that has no saved selection.
    agent_backend: str = field(default_factory=lambda: config.agent_backend)
    # Cached short summaries for ClaudeSession picker. Key = claude session id.
    # Value = {"summary": str, "mtime": float, "ts": float}.
    summary_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Window ids that were just `claude --resume`d and may still be
    # auto-compacting. While a window is here, ``send_to_window`` buffers
    # prompts into ``_pending_sends`` instead of typing them, and a
    # background task in ``_resume_settle_tasks`` drains the buffer once
    # the pane settles (see ``_watch_resume_settle``).
    # In-memory only — never persisted; a restart means compaction has long
    # finished anyway.
    _resuming_windows: set[str] = field(default_factory=set)
    # Prompts buffered while a window is mid-resume. Drained in arrival
    # order by ``_watch_resume_settle`` after the pane goes idle.
    _pending_sends: dict[str, list[str]] = field(default_factory=dict)
    # Background watcher tasks, one per resuming window.
    _resume_settle_tasks: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def save_state(self) -> None:
        # Local import to avoid an init-time cycle: bg_status imports
        # session_manager, which is constructed by importing this module.
        from .handlers import bg_status

        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "active_sessions": {
                str(uid): sid for uid, sid in self.active_sessions.items()
            },
            "active_history": {
                str(uid): hist for uid, hist in self.active_history.items()
            },
            "sessions": {sid: s.to_dict() for sid, s in self.sessions.items()},
            "last_switcher_msg_id": {
                str(uid): mid for uid, mid in self.last_switcher_msg_id.items()
            },
            "card_msg_id": {str(uid): mid for uid, mid in self.card_msg_id.items()},
            "window_display_names": self.window_display_names,
            "user_settings": {
                str(uid): vals for uid, vals in self.user_settings.items()
            },
            "agent_backend": self.agent_backend,
            "summary_cache": self.summary_cache,
            "bg_status": bg_status.serialize_per_user(),
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
                self.active_history = {
                    int(uid): list(hist)
                    for uid, hist in state.get("active_history", {}).items()
                    if isinstance(hist, list)
                }
                self.sessions = {
                    sid: Session.from_dict(data)
                    for sid, data in state.get("sessions", {}).items()
                }
                self.last_switcher_msg_id = {
                    int(uid): int(mid)
                    for uid, mid in state.get("last_switcher_msg_id", {}).items()
                }
                self.card_msg_id = {
                    int(uid): int(mid)
                    for uid, mid in state.get("card_msg_id", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.user_settings = {
                    int(uid): dict(vals)
                    for uid, vals in state.get("user_settings", {}).items()
                }
                saved_backend = state.get("agent_backend", config.agent_backend)
                self.agent_backend = (
                    saved_backend
                    if saved_backend in ("claude", "codex")
                    else config.agent_backend
                )
                # Keep legacy call sites and helper modules in sync with the
                # persisted bot-wide selection.
                config.agent_backend = self.agent_backend
                self.summary_cache = dict(state.get("summary_cache", {}))

                # Late import — handlers package imports session_manager.
                from .handlers import bg_status

                bg_status.load_per_user(state.get("bg_status"))

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
                self.card_msg_id = {}
                self.window_display_names = {}
                self.user_settings = {}
                self.summary_cache = {}
                self.agent_backend = config.agent_backend

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

    # --- Tmux helpers ---

    def mark_window_resuming(
        self,
        window_id: str,
        *,
        bot: "Bot | None" = None,
        user_id: int | None = None,
    ) -> None:
        """Flag a window as freshly ``--resume``d and spawn the settle watcher.

        Prompts that arrive while the window is flagged are buffered into
        ``_pending_sends`` by ``send_to_window`` and drained by
        ``_watch_resume_settle`` once the pane goes idle — the message
        handler never blocks on the wait. When ``bot``/``user_id`` are
        supplied, the watcher also keeps Telegram's TYPING indicator alive
        so the chat doesn't look frozen during a long compaction.
        """
        self.mark_window_starting(
            window_id,
            backend="claude",
            resume=True,
            bot=bot,
            user_id=user_id,
        )

    def mark_window_starting(
        self,
        window_id: str,
        *,
        backend: str,
        resume: bool,
        bot: "Bot | None" = None,
        user_id: int | None = None,
    ) -> None:
        """Gate sends until a newly-created agent pane can accept input.

        The Session record and Telegram card may be published immediately;
        ``send_to_window`` queues prompts in arrival order while this watcher
        waits for the real TUI input prompt. ``resume=True`` preserves the
        extra Claude compaction settle window.
        """
        if config.resume_settle_timeout <= 0:
            return
        if window_id in self._resuming_windows:
            return  # already being watched
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called outside an event loop (e.g. sync test setup) — keep
            # the flag-only fallback so existing behavior is preserved.
            self._resuming_windows.add(window_id)
            return
        self._resuming_windows.add(window_id)
        self._resume_settle_tasks[window_id] = loop.create_task(
            self._watch_resume_settle(
                window_id, bot, user_id, backend=backend, resume=resume
            ),
            name=f"startup-ready:{window_id}",
        )

    async def _watch_resume_settle(
        self,
        window_id: str,
        bot: "Bot | None",
        user_id: int | None,
        *,
        backend: str,
        resume: bool,
    ) -> None:
        """Background watcher for a resuming window.

        Polls the pane via ``_wait_for_resume_settle`` until it settles
        (or the configured timeout elapses), then drains anything
        ``send_to_window`` buffered into ``_pending_sends`` while the
        gate was up. Concurrently keeps Telegram's TYPING indicator
        refreshed so the user sees the bot is still working.
        """
        stop_typing = asyncio.Event()

        async def _typing_keepalive() -> None:
            if bot is None or user_id is None:
                return
            # Local import — handlers.typing pulls in telegram modules
            # and ``session`` is imported very early.
            from .handlers.typing import fire_typing

            while not stop_typing.is_set():
                try:
                    await fire_typing(
                        bot, user_id, "resume_settle", window_id=window_id
                    )
                except Exception as e:
                    logger.debug("resume-settle typing keepalive failed: %s", e)
                try:
                    await asyncio.wait_for(
                        stop_typing.wait(), timeout=_RESUME_SETTLE_TYPING_REFRESH
                    )
                except asyncio.TimeoutError:
                    pass

        keepalive_task = asyncio.create_task(
            _typing_keepalive(), name=f"resume-settle-typing:{window_id}"
        )
        try:
            settled: bool | None = False
            while settled is False:
                settled = await self._wait_for_resume_settle(
                    window_id, backend=backend, resume=resume
                )
                if settled is None:
                    logger.warning(
                        "startup gate abandoned for vanished window %s",
                        window_id,
                    )
                    return
                if settled is False:
                    logger.error(
                        "startup gate remains closed for window %s; "
                        "TUI readiness is still unproven",
                        window_id,
                    )
            logger.info(
                "startup gate cleared for window %s "
                "(settled=%s backend=%s resume=%s, background)",
                window_id,
                settled,
                backend,
                resume,
            )
            drained = 0
            while True:
                pending = self._pending_sends.pop(window_id, [])
                if not pending:
                    # No await between the final empty check and clearing the
                    # gate: a concurrent send either joined the batch above or
                    # observes the cleared gate and sends normally.
                    self._resuming_windows.discard(window_id)
                    break
                for text in pending:
                    ok = await tmux_manager.send_keys(window_id, text)
                    if ok and backend == "codex":
                        ok = await tmux_manager.ensure_codex_prompt_submitted(
                            window_id, text
                        )
                    if not ok:
                        logger.warning(
                            "startup gate: failed to drain pending send #%d "
                            "for window %s (text_len=%d)",
                            drained,
                            window_id,
                            len(text),
                        )
                    drained += 1
                    # Give the TUI one render tick before submitting another
                    # queued prompt. A new arrival during this sleep is picked
                    # up by the next outer-loop batch.
                    await asyncio.sleep(_RESUME_SETTLE_DRAIN_GAP)
            if drained:
                logger.info(
                    "startup gate: drained %d pending send(s) for window %s",
                    drained,
                    window_id,
                )
        except Exception as e:
            logger.exception(
                "resume-settle watcher failed for window %s: %s", window_id, e
            )
        finally:
            stop_typing.set()
            try:
                await keepalive_task
            except Exception:
                pass
            self._resuming_windows.discard(window_id)
            self._resume_settle_tasks.pop(window_id, None)
            # Belt-and-suspenders: on exception path, drop any prompts
            # that didn't get drained so they can't leak forever.
            self._pending_sends.pop(window_id, None)

    @staticmethod
    def _pane_has_ready_input(pane: str, backend: str) -> bool:
        """Whether the visible pane ends in the agent's real input box."""
        if not pane or is_interactive_ui(pane) or parse_status_line(pane) is not None:
            return False
        lower = pane.lower()
        if backend == "codex" and (
            "do you trust the contents of this directory?" in lower
            or "choose working directory to resume this session" in lower
            or "sign in with chatgpt" in lower
            or "sign in with device code" in lower
            or "provide your own api key" in lower
            or "update available!" in lower
        ):
            return False
        marker = "›" if backend == "codex" else "❯"
        lines = pane.strip().splitlines()
        if backend == "codex" and "openai codex" not in lower:
            # A fresh pane exposes the OpenAI Codex header, but a resumed long
            # transcript scrolls that header out of capture-pane before the
            # input becomes ready.  Its bottom status row is still stable:
            # ``<model> <effort> · <cwd>``.  Accept that as Codex evidence so
            # resume cannot remain gated forever, while still rejecting
            # Artem's shell prompt (which also starts with ``›``).
            efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
            has_codex_footer = False
            for line in lines[-8:]:
                parts = [part.strip() for part in line.split("·")]
                if len(parts) < 2:
                    continue
                model_effort = parts[0].split()
                if not model_effort or model_effort[-1].lower() not in efforts:
                    continue
                if any(
                    part == "~" or part.startswith(("~/", "/")) for part in parts[1:]
                ):
                    has_codex_footer = True
                    break
            if not has_codex_footer:
                return False
        # The live input row is pinned near the bottom. Restricting detection
        # to the tail avoids mistaking a historical user row for readiness
        # while a resumed transcript is still being restored.
        return any(line.lstrip().startswith(marker) for line in lines[-6:])

    async def wait_for_window_ready(self, window_id: str) -> bool:
        """Wait until the startup gate has observed the real agent input UI."""
        while window_id in self._resuming_windows:
            task = self._resume_settle_tasks.get(window_id)
            if task is None:
                await asyncio.sleep(_RESUME_SETTLE_POLL)
                continue
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                return False
            except Exception:
                logger.exception("startup readiness task failed: %s", window_id)
                return False
        window = await tmux_manager.find_window_by_id(window_id)
        return window is not None

    async def _wait_for_resume_settle(
        self,
        window_id: str,
        *,
        backend: str = "claude",
        resume: bool = True,
    ) -> bool | None:
        """Block until a just-resumed window is safe to type into.

        A ``claude --resume`` of a near-limit transcript auto-compacts before
        it accepts input — 60-110s on ~1M-token sessions. Typing a prompt
        into the pane mid-compaction silently drops it, so the first send
        holds here. Two ways to declare "settled":

          * the pane showed a busy spinner (load / compaction) and it has
            now been gone for ``_RESUME_SETTLE_IDLE_STABLE`` seconds, or
          * no busy spinner appeared within ``_RESUME_SETTLE_BUSY_GRACE``
            seconds (small session — nothing to compact).

        Returns True when settled, False on timeout (the watcher retries so
        queued startup messages are not lost), and None when the tmux window
        vanished while it was being watched.
        """
        loop = asyncio.get_event_loop()
        started = loop.time()
        deadline = started + config.resume_settle_timeout
        saw_busy = False
        idle_since: float | None = None
        while loop.time() < deadline:
            pane = await tmux_manager.capture_pane(window_id)
            if pane is None:
                window = await tmux_manager.find_window_by_id(window_id)
                if window is None:
                    return None
            now = loop.time()
            busy = bool(pane) and parse_status_line(pane) is not None
            ready = bool(pane) and self._pane_has_ready_input(pane or "", backend)
            if busy:
                saw_busy = True
                idle_since = None
            else:
                # Fresh sessions and Codex resumes are ready the moment the
                # actual input box appears. Claude resume keeps the historical
                # grace/stability rule because compaction may start shortly
                # after an initially-idle frame.
                if ready and (not resume or backend == "codex"):
                    return True
                if not ready:
                    idle_since = None
                    await asyncio.sleep(_RESUME_SETTLE_POLL)
                    continue
                if idle_since is None:
                    idle_since = now
                if saw_busy and (now - idle_since) >= _RESUME_SETTLE_IDLE_STABLE:
                    return True
                if not saw_busy and (now - started) >= _RESUME_SETTLE_BUSY_GRACE:
                    return True
            await asyncio.sleep(_RESUME_SETTLE_POLL)
        logger.warning(
            "resume-settle timed out for window %s after %.0fs (saw_busy=%s); "
            "readiness remains unproven",
            window_id,
            config.resume_settle_timeout,
            saw_busy,
        )
        return False

    def cancel_window_startup(self, window_id: str) -> None:
        """Cancel a readiness gate after its tmux window was rolled back."""
        task = self._resume_settle_tasks.pop(window_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._resuming_windows.discard(window_id)
        self._pending_sends.pop(window_id, None)

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a tmux window by ID.

        For newly-created windows whose TUI is still booting, the text is
        buffered into ``_pending_sends`` and success is returned immediately.
        The background watcher drains the buffer in arrival order once the
        real input prompt appears. This covers both ordinary startup and a
        long resume compaction without holding the Telegram handler open.
        """
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
        if window_id in self._resuming_windows:
            queue = self._pending_sends.setdefault(window_id, [])
            queue.append(text)
            logger.info(
                "send_to_window buffered: window=%s pending=%d text_len=%d "
                "(startup in progress)",
                window_id,
                len(queue),
                len(text),
            )
            return True, f"Queued for {display} (session starting)"
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
