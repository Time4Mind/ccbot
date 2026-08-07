"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Claude Code windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / kill_window: lifecycle management.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import libtmux

from . import tmux_window as _tmux_window
from .config import SENSITIVE_ENV_VARS, config
from .tmux_process import kill_orphan_processes

logger = logging.getLogger(__name__)

# Validate before passing to pgrep so we never inject arbitrary regex
# into the command line. claude session ids are UUIDs.
_CLAUDE_SESSION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_CODEX_TRUST_PROMPT = "Do you trust the contents of this directory?"
_CODEX_TRUST_YES = "1. Yes, continue"


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None
        # One lock per window id, serializing send_keys on that pane. The
        # literal+enter path types the text, sleeps ~0.5s, then presses
        # Enter — without a lock a second concurrent send (e.g. a voice
        # transcription landing while the user just typed text) injects
        # its keystrokes into the same input box during that gap, merging
        # both messages into one and firing a spurious Enter. See
        # send_keys.
        self._send_locks: dict[str, asyncio.Lock] = {}
        # Codex directory-trust handling used to block ``create_window`` for
        # up to 4.5 seconds. Keep the pollers alive in the background instead;
        # session readiness/queued input is handled independently by
        # SessionManager's startup gate.
        self._startup_tasks: set[asyncio.Task[bool]] = set()

    def _send_lock_for(self, window_id: str) -> asyncio.Lock:
        """Return the per-window send lock, creating it on first use.

        Safe to call without external synchronization: this runs on the
        single asyncio event loop thread, so the get-or-create is atomic
        with respect to other coroutines.
        """
        lock = self._send_locks.get(window_id)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[window_id] = lock
        return lock

    @staticmethod
    def _handle_codex_startup_screen(pane: object) -> tuple[bool, bool]:
        """Handle one captured Codex startup screen."""
        return _tmux_window.handle_codex_startup_screen(
            pane,
            trust_prompt=_CODEX_TRUST_PROMPT,
            trust_yes=_CODEX_TRUST_YES,
            logger_obj=logger,
        )

    @classmethod
    def _accept_codex_directory_trust(cls, pane: object) -> bool:
        """Synchronously handle known Codex startup prompts."""
        return _tmux_window.accept_codex_directory_trust(
            pane,
            sleep=time.sleep,
            handler=cls._handle_codex_startup_screen,
        )

    @classmethod
    async def _watch_codex_startup_screens(cls, pane: object) -> bool:
        """Cancellation-safe long watcher for cold Codex launches."""
        return await _tmux_window.watch_codex_startup_screens(
            pane,
            timeout=config.resume_settle_timeout,
            sleep=asyncio.sleep,
            to_thread=asyncio.to_thread,
            handler=cls._handle_codex_startup_screen,
        )

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        Returns:
            List of TmuxWindow with window info and cwd
        """

        def _sync_list_windows() -> list[TmuxWindow]:
            windows = []
            session = self.get_session()

            if not session:
                return windows

            for window in session.windows:
                name = window.window_name or ""
                # Skip the main window (placeholder window)
                if name == config.tmux_main_window_name:
                    continue

                try:
                    # Get the active pane's current path and command
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                    else:
                        cwd = ""
                        pane_cmd = ""

                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            window_name=name,
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error getting window info: {e}")

            return windows

        return await asyncio.to_thread(_sync_list_windows)

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes

        Returns:
            The captured text, or None on failure.
        """
        if with_ansi:
            # Use async subprocess to call tmux capture-pane -e for ANSI colors
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "capture-pane",
                    "-e",
                    "-p",
                    "-t",
                    window_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout.decode("utf-8")
                logger.error(
                    f"Failed to capture pane {window_id}: {stderr.decode('utf-8')}"
                )
                return None
            except Exception as e:
                logger.error(f"Unexpected error capturing pane {window_id}: {e}")
                return None

        # Original implementation for plain text - wrap in thread
        def _sync_capture() -> str | None:
            session = self.get_session()
            if not session:
                return None
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return None
                pane = window.active_pane
                if not pane:
                    return None
                lines = pane.capture_pane()
                return (
                    "\n".join(lines)
                    if isinstance(lines, list)  # pyright: ignore[reportUnnecessaryIsInstance]
                    else str(lines)
                )
            except Exception as e:
                logger.error(f"Failed to capture pane {window_id}: {e}")
                return None

        return await asyncio.to_thread(_sync_capture)

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise

        Serialized per window: the literal+enter path types the text, waits
        ~0.5s, then presses Enter. A concurrent send on the same pane during
        that gap (classically a voice transcription completing right as the
        user typed text into the same session) would otherwise interleave
        its keystrokes into the same input box — both messages merge into
        one submission and one of the two Enters lands on an empty prompt.
        The lock makes each send atomic against every other send on the pane.
        """
        async with self._send_lock_for(window_id):
            return await self._send_keys_locked(window_id, text, enter, literal)

    async def _send_keys_locked(
        self, window_id: str, text: str, enter: bool, literal: bool
    ) -> bool:
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    pane.send_keys(chars, enter=False, literal=True)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send Enter to window {window_id}: {e}")
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    @staticmethod
    def _codex_prompt_contains(pane_text: str, text: str) -> bool:
        """Whether Codex still shows ``text`` in its bottom input prompt."""
        lines = pane_text.splitlines()
        prompt_idx = -1
        # Long voice transcriptions can wrap to far more than 20 terminal
        # rows.  Looking only at the pane tail then misses the opening ``>``
        # even though the entire transcription is still sitting unsent in
        # the input field.  The last prompt marker in the captured pane is
        # the live input; submitted turns are followed by a newer, empty
        # prompt once Codex returns to idle.
        for i in range(len(lines) - 1, -1, -1):
            if re.match(r"^\s*[›>](?:\s|$)", lines[i]):
                prompt_idx = i
                break
        if prompt_idx < 0:
            return False
        if any(
            re.search(r"\bWorking\b|esc to interrupt", line, re.IGNORECASE)
            for line in lines[prompt_idx + 1 :]
        ):
            return False
        prompt = " ".join(" ".join(lines[prompt_idx:]).split())
        needle = " ".join(text.split())
        if not needle:
            return False
        if len(needle) <= 80:
            return needle in prompt
        # A long TUI input may have scrolled its beginning out of the pane.
        return needle[-64:] in prompt or needle[:64] in prompt

    async def ensure_codex_prompt_submitted(self, window_id: str, text: str) -> bool:
        """Retry Enter while Codex leaves the just-typed prompt pending.

        ``send_keys`` can successfully deliver bytes while Codex treats the
        first Enter as an input-layout event.  Verify the bottom prompt after
        the TUI has had a tick; before every retry, require the exact text to
        still be present. This avoids duplicate turns while tolerating more
        than one swallowed Enter (observed with long voice transcriptions).
        """
        # Give Codex enough time to process a large bracketed paste before
        # deciding that the first Enter was swallowed by the TUI.
        await asyncio.sleep(2.0)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            pane = await self.capture_pane(window_id)
            if not pane or not self._codex_prompt_contains(pane, text):
                return True
            logger.warning(
                "Codex prompt still pending in %s; retrying Enter (%d/%d)",
                window_id,
                attempt,
                max_retries,
            )
            if not await self.send_keys(window_id, "Enter", enter=False, literal=False):
                return False
            await asyncio.sleep(2.0)
        pane = await self.capture_pane(window_id)
        return not pane or not self._codex_prompt_contains(pane, text)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_rename)

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID.

        Also tears down the matching per-window grouped session
        (``<source>-w<wid>``) that ``local_terminal`` may have created.
        Without this the group would linger as an empty view of the
        remaining source windows, which both pollutes
        ``list-sessions`` and makes the next reuse of the same window
        id reattach to a confused leftover.
        """

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                self._kill_grouped_session_for_window(window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        killed = await asyncio.to_thread(_sync_kill)
        # Drop the per-window send lock — tmux never reuses window ids, so
        # the entry would otherwise linger for the process lifetime.
        self._send_locks.pop(window_id, None)
        return killed

    def has_client_for_window(self, window_id: str) -> bool:
        """True iff some tmux client is attached to this window's group session.

        Each local terminal lives on a per-window grouped session named
        ``<source>-w<wid>`` (see ``local_terminal._build_tmux_command``).
        The Menu's "Open terminal" button uses this to hide itself when
        the user already has a terminal pointed at the active session.

        ``server.cmd`` returns one ``list-clients`` line per attached
        client; an empty stdout means "group exists but unattached" or
        "group does not exist", both of which mean we should show the
        button.
        """
        suffix = window_id.lstrip("@")
        if not suffix:
            return False
        target = f"{self.session_name}-w{suffix}"
        try:
            result = self.server.cmd("list-clients", "-t", target)
        except Exception:
            return False
        stdout = result.stdout if hasattr(result, "stdout") else []
        if not stdout:
            return False
        # libtmux returns stdout as a list of lines.
        return any(line.strip() for line in stdout)

    def _kill_grouped_session_for_window(self, window_id: str) -> None:
        """Kill the ``<source>-w<wid>`` grouped session, if present.

        Synchronous helper for ``kill_window`` — runs in the same
        worker thread so the cleanup happens atomically with the
        window kill.
        """
        suffix = window_id.lstrip("@")
        if not suffix:
            return
        target = f"{self.session_name}-w{suffix}"
        try:
            grouped = self.server.sessions.get(session_name=target)
        except Exception:
            return
        if grouped is None:
            return
        try:
            grouped.kill()
            logger.info("Killed grouped session %s", target)
        except Exception as e:
            logger.debug("kill grouped session %s failed: %s", target, e)

    async def kill_orphan_claude_processes(self, claude_session_id: str) -> int:
        """SIGTERM any surviving claude resume process for this session."""
        if not _CLAUDE_SESSION_RE.match(claude_session_id):
            logger.warning(
                "kill_orphan_claude_processes: invalid session id %r, skipping",
                claude_session_id,
            )
            return 0
        return await asyncio.to_thread(
            kill_orphan_processes,
            claude_session_id,
            run=subprocess.run,
            kill=os.kill,
            own_pid=os.getpid(),
            parent_pid=os.getppid(),
            sigterm=signal.SIGTERM,
            timeout_error=subprocess.TimeoutExpired,
            logger=logger,
        )

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        owner_user_id: int | None = None,
        backend: str | None = None,
        initial_prompt: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a tmux window and optionally start the configured agent."""
        return await _tmux_window.create_window(
            self,
            work_dir,
            window_name=window_name,
            start_claude=start_claude,
            resume_session_id=resume_session_id,
            owner_user_id=owner_user_id,
            backend=backend,
            initial_prompt=initial_prompt,
            config_obj=config,
            logger_obj=logger,
        )


# Global instance with default session name
tmux_manager = TmuxManager()
