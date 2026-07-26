"""Claude Code OAuth re-login driven from Telegram.

When a session dies with an expired OAuth login there is no way to fix it from
the phone unless the bot itself can run the login exchange — this module is
that mechanic. It owns a ``claude auth login`` child process, hands its OAuth
URL to the chat, and feeds the code the user pastes back into the *same*
process (the PKCE verifier lives in that process's memory, so the exchange
cannot be split across two invocations).

Measured against Claude Code v2.1.220:

* the login prints ``If the browser didn't open, visit: <url>`` and then waits
  on ``Paste code here if prompted >`` — the URL's ``redirect_uri`` is
  ``platform.claude.com``, not a localhost loopback, so a phone browser
  completes the redirect and shows a code to copy;
* a pipe never streams that output (the CLI block-buffers and the prompt has
  no trailing newline), so the child runs on a **pty**, sized wide enough that
  the URL is not wrapped across lines;
* a dead login surfaces as ``Failed to authenticate: OAuth session expired and
  could not be refreshed``, written to the JSONL as a synthetic assistant
  turn flagged ``isApiErrorMessage: true`` with ``error: authentication_failed``
  — that flag, not the wording, is what ``is_auth_failure_event`` trusts.

Core responsibilities:
  - recognise Claude Code's own auth-failure turn (``is_auth_failure_event``)
  - read the credential store's deadlines (``credentials_state``)
  - run the login exchange (``LoginFlow``) with a per-user registry + TTL

Key components: LoginFlow, start_flow, get_flow, drop_flow, credentials_state.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Entry-level ``error`` code Claude Code writes for a dead login (observed on
# v2.1.220 alongside ``isApiErrorMessage: true`` and ``model: "<synthetic>"``).
AUTH_ERROR_CODE = "authentication_failed"

# Wording of the same failure, used only as a fallback and only when the text
# *is* the error line (see ``is_auth_failure_event``).
AUTH_FAILURE_MARKERS: tuple[str, ...] = (
    "Login expired",
    "OAuth session expired",
    "Failed to authenticate",
    "Please run /login",
)
# The real error line is one short sentence; anything longer is prose about it.
_MAX_ERROR_TEXT = 120

_URL_RE = re.compile(r"https://claude\.com/cai/oauth/authorize\?[^\s\x1b\]]+")
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]|\x1b\][0-9]+;[^\x07]*\x07"
)
_SUCCESS_MARKERS: tuple[str, ...] = ("Login successful", "Logged in", "successfully")
_FAILURE_MARKERS: tuple[str, ...] = ("Invalid code", "failed", "error", "Error")

# A flow holds a child process open while the user is in the browser; drop it
# after this long so a forgotten flow can't linger and swallow a later message.
FLOW_TTL = 15 * 60.0
# Sizing the pty wide keeps the OAuth URL on one line.
_PTY_COLS = 400
_URL_WAIT = 90.0
_CODE_WAIT = 120.0


def is_auth_failure_event(api_error: str, text: str) -> bool:
    """True only for Claude Code's own synthetic auth-failure turn.

    ``api_error`` is the JSONL entry-level code (``error`` alongside
    ``isApiErrorMessage: true``) — that flag is the whole point. Matching the
    error *wording* against arbitrary assistant text is not good enough: a
    session that merely discusses the failure (this feature's own development
    session did) would trip the notice and offer to re-authenticate a perfectly
    healthy host.
    """
    if not api_error:
        return False
    if api_error == AUTH_ERROR_CODE:
        return True
    # Older/other builds may only set a generic code, so fall back to the
    # wording — but only for text that *is* the error, not text containing it.
    stripped = (text or "").strip()
    if len(stripped) > _MAX_ERROR_TEXT:
        return False
    return any(stripped.startswith(marker) for marker in AUTH_FAILURE_MARKERS)


def config_dir() -> Path:
    """The credential store this bot's claude processes use."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


@dataclass
class CredentialsState:
    """Deadlines read out of ``.credentials.json``; all times are epoch seconds."""

    present: bool
    access_expires_at: float | None = None
    # Absolute wall: rotation issues a new refresh token but keeps this value,
    # so it only moves on a fresh interactive login.
    refresh_expires_at: float | None = None
    subscription: str = ""

    @property
    def refresh_alive(self) -> bool:
        return bool(self.refresh_expires_at and self.refresh_expires_at > time.time())


def credentials_state(path: Path | None = None) -> CredentialsState:
    """Read the credential store without touching the tokens themselves."""
    creds = (path or config_dir()) / ".credentials.json"
    try:
        blob = json.loads(creds.read_text())
    except (OSError, json.JSONDecodeError):
        return CredentialsState(present=False)
    oauth = blob.get("claudeAiOauth") or {}
    if not oauth:
        return CredentialsState(present=False)
    access = oauth.get("expiresAt")
    refresh = oauth.get("refreshTokenExpiresAt")
    return CredentialsState(
        present=True,
        access_expires_at=access / 1000 if access else None,
        refresh_expires_at=refresh / 1000 if refresh else None,
        subscription=str(oauth.get("subscriptionType") or ""),
    )


def _strip(raw: str) -> str:
    return _ANSI_RE.sub("", raw)


class LoginFlow:
    """One ``claude auth login`` exchange, awaiting a pasted code."""

    def __init__(self, user_id: int, command: str = "claude") -> None:
        self.user_id = user_id
        self.command = command
        self.created_at = time.time()
        self.url: str = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._master: int | None = None

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > FLOW_TTL

    # --- lifecycle -----------------------------------------------------

    def _spawn(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, _PTY_COLS, 0, 0))
        env = dict(os.environ)
        # Never let the login inherit the bot's tmux identity: the ccbot
        # SessionStart hook would otherwise rewrite session_map.json and point
        # a live window at this throwaway process.
        env.pop("TMUX", None)
        env["IS_SANDBOX"] = "1"
        self._proc = subprocess.Popen(
            (self.command, "auth", "login"),
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        self._master = master

    def _drain(self, deadline: float, stop: re.Pattern[str] | tuple[str, ...]) -> str:
        """Read the pty until `stop` matches the cleaned buffer, or time runs out."""
        assert self._master is not None
        buf = ""
        while time.time() < deadline:
            try:
                ready, _, _ = select.select([self._master], [], [], 0.5)
            except OSError:
                break
            if not ready:
                continue
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += _strip(chunk.decode(errors="replace"))
            if isinstance(stop, re.Pattern):
                if stop.search(buf):
                    break
            elif any(marker in buf for marker in stop):
                break
        return buf

    async def start(self) -> str:
        """Spawn the login and return its OAuth URL (empty string on failure)."""
        await asyncio.to_thread(self._spawn)
        buf = await asyncio.to_thread(self._drain, time.time() + _URL_WAIT, _URL_RE)
        match = _URL_RE.search(buf)
        if not match:
            logger.warning("login flow: no OAuth URL captured; tail=%r", buf[-300:])
            self.cancel()
            return ""
        self.url = match.group(0)
        return self.url

    async def submit_code(self, code: str) -> tuple[bool, str]:
        """Feed the pasted code in. Returns (ok, detail-for-the-user)."""
        if self._master is None or self._proc is None:
            return False, "flow is gone"
        before = credentials_state().refresh_expires_at
        try:
            await asyncio.to_thread(os.write, self._master, (code + "\r").encode())
        except OSError as exc:
            self.cancel()
            return False, f"could not send the code: {exc}"
        buf = await asyncio.to_thread(
            self._drain, time.time() + _CODE_WAIT, _SUCCESS_MARKERS + _FAILURE_MARKERS
        )
        after = credentials_state()
        self.cancel()
        # The credential store is the source of truth: a successful exchange
        # rewrites it with a fresh wall. Wording in the CLI output may change,
        # the file contract has been stable.
        moved = bool(
            after.refresh_expires_at
            and (before is None or after.refresh_expires_at > before)
        )
        if moved and after.refresh_alive:
            return True, ""
        tail = " ".join(buf.split())[-200:]
        return False, tail or "the CLI did not confirm the login"

    def cancel(self) -> None:
        """Kill the child and close the pty. Safe to call more than once."""
        proc, master = self._proc, self._master
        self._proc, self._master = None, None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass


# --- per-user registry ------------------------------------------------

_flows: dict[int, LoginFlow] = {}


def get_flow(user_id: int) -> LoginFlow | None:
    """The user's pending flow, dropping it first if it timed out."""
    flow = _flows.get(user_id)
    if flow is None:
        return None
    if flow.expired:
        logger.info("login flow for %s expired, dropping", user_id)
        drop_flow(user_id)
        return None
    return flow


def drop_flow(user_id: int) -> None:
    flow = _flows.pop(user_id, None)
    if flow is not None:
        flow.cancel()


async def start_flow(user_id: int, command: str = "claude") -> LoginFlow | None:
    """Replace any pending flow with a fresh one that already has its URL."""
    drop_flow(user_id)
    flow = LoginFlow(user_id, command=command)
    url = await flow.start()
    if not url:
        return None
    _flows[user_id] = flow
    return flow
