"""Open a native macOS Terminal.app / iTerm window attached to a tmux window.

The bot already runs inside a real ``tmux`` server on the host, so any
shell on the same machine can ``tmux attach -t ccbot``. This helper
just automates that — when the user enables ``local_terminal`` in
Settings, every freshly-created Claude session also pops a native
Terminal window pointed at its tmux window, so the user can drive the
session by hand without typing the attach command each time.

Public API:
  open_terminal_for_window(window_id) -> None
      No-op when not on macOS, when the user setting is off, or when
      AppleScript spawn fails. Logs only — never raises.

Implementation: shells out to ``osascript``. iTerm is preferred when
already running; falls back to Terminal.app otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shlex

from .config import config

logger = logging.getLogger(__name__)


def _terminal_app_args(tmux_cmd: str) -> list[str]:
    """AppleScript: focus Terminal.app, open a new window with `tmux_cmd`.

    ``do script`` opens a fresh window if Terminal isn't running, or a new
    tab/window in an existing instance.
    """
    return [
        "osascript",
        "-e",
        'tell application "Terminal" to activate',
        "-e",
        f'tell application "Terminal" to do script {_quote_applescript(tmux_cmd)}',
    ]


def _iterm_args(tmux_cmd: str) -> list[str]:
    """AppleScript: open the tmux attach command as a new tab of the
    front-most iTerm window if one exists, else create a new window.

    iTerm2 supports tabs cleanly via AppleScript; we prefer them so the
    user doesn't end up with a wall of independent windows after several
    sessions.
    """
    quoted = _quote_applescript(tmux_cmd)
    script = (
        'tell application "iTerm"\n'
        "    activate\n"
        "    if (count of windows) = 0 then\n"
        f"        create window with default profile command {quoted}\n"
        "    else\n"
        "        tell current window\n"
        f"            create tab with default profile command {quoted}\n"
        "        end tell\n"
        "    end if\n"
        "end tell"
    )
    return ["osascript", "-e", script]


def _quote_applescript(text: str) -> str:
    """Wrap a string for AppleScript: double-quoted with backslash escapes."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


async def _is_iterm_running() -> bool:
    """Best-effort detect: is iTerm currently in the running-processes list?"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            'tell application "System Events" to (name of processes) '
            'contains "iTerm2"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out.strip() == b"true"
    except Exception as e:
        logger.debug("local_terminal: iTerm probe failed: %s", e)
        return False


def _build_tmux_command(window_id: str) -> str:
    """`tmux attach -t <session> \\; select-window -t @<wid>`."""
    session = shlex.quote(config.tmux_session_name)
    # tmux's literal `\;` separates commands inside a single attach call;
    # AppleScript will receive it verbatim through our quoting helper.
    return f"tmux attach -t {session} \\; select-window -t {window_id}"


async def open_terminal_for_window(window_id: str) -> None:
    """Pop a native Terminal/iTerm window attached to the given tmux window.

    Silent on non-macOS hosts and on osascript failure — this is a UX
    convenience, never load-bearing.
    """
    if platform.system() != "Darwin":
        logger.debug(
            "local_terminal: only macOS supported (current=%s)", platform.system()
        )
        return
    if not window_id:
        return

    tmux_cmd = _build_tmux_command(window_id)
    args = (
        _iterm_args(tmux_cmd)
        if await _is_iterm_running()
        else _terminal_app_args(tmux_cmd)
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "local_terminal: osascript failed (rc=%d): %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
        else:
            logger.info("local_terminal: opened for window %s", window_id)
    except Exception as e:
        logger.warning("local_terminal: spawn failed: %s", e)
