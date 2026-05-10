"""Open a native Terminal/iTerm/<linux-emu> window attached to a tmux window.

The bot already runs inside a real ``tmux`` server on the host, so any
shell on the same machine can ``tmux attach -t ccbot``. This helper
just automates that — when the user enables ``local_terminal`` in
Settings, every freshly-created Claude session also pops a native
window pointed at its tmux window so the user can drive the session
by hand without typing the attach command each time.

Public API:
  open_terminal_for_window(window_id, user_id) -> None
      No-op when the platform is unsupported, when the user setting is
      off, when no Linux emulator is configured, or when the spawn
      fails. Logs only — never raises.
  detect_linux_emulators() -> list[str]
      Names of known Linux emulators currently on PATH.
  LINUX_TEMPLATES
      Public mapping of known emulator → command template. Templates
      contain a ``{shell}`` placeholder that ``_expand_linux_template``
      replaces with a single shell-quoted ``tmux attach`` snippet.

macOS: shells out to ``osascript``. iTerm is preferred when already
running and uses tabs; Terminal.app is the fallback.

Linux: runs the user-selected ``local_terminal_cmd`` template, or the
``CCBOT_LOCAL_TERMINAL_CMD`` env var as a fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
import shutil

from .config import config

logger = logging.getLogger(__name__)


# Known Linux emulators and the command templates we use to launch them.
# ``{shell}`` is replaced with one shell-quoted argument that runs
# ``tmux attach -t <session> \; select-window -t @<wid>`` followed by an
# interactive ``bash`` so the window doesn't snap shut on detach.
LINUX_TEMPLATES: dict[str, str] = {
    "gnome-terminal": "gnome-terminal -- bash -c {shell}",
    "konsole": "konsole --new-tab -e bash -c {shell}",
    "kitty": "kitty bash -c {shell}",
    "wezterm": "wezterm start -- bash -c {shell}",
    "alacritty": "alacritty -e bash -c {shell}",
    "tilix": "tilix -e bash -c {shell}",
    "foot": "foot bash -c {shell}",
    "xterm": "xterm -e bash -c {shell}",
}


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
            'tell application "System Events" to (name of processes) contains "iTerm2"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out.strip() == b"true"
    except Exception as e:
        logger.debug("local_terminal: iTerm probe failed: %s", e)
        return False


def _build_tmux_command(window_id: str) -> str:
    """tmux attach + select-window + interactive-shell tail.

    ``\\;`` is tmux's command separator (bash escapes it through to tmux's
    argv). The trailing ``|| true; exec bash -l`` mirrors what the Linux
    helper does — it keeps the window open after the user detaches from
    tmux (or after attach fails for any reason). Without it, iTerm tabs
    and Terminal.app windows configured to close on shell exit will snap
    shut the moment the user types Ctrl-b d.
    """
    session = shlex.quote(config.tmux_session_name)
    return (
        f"tmux attach -t {session} \\; select-window -t {window_id} "
        f"|| true; exec bash -l"
    )


def detect_linux_emulators() -> list[str]:
    """Names of known Linux terminal emulators currently on PATH."""
    return [name for name in LINUX_TEMPLATES if shutil.which(name) is not None]


def _build_linux_shell_cmd(window_id: str) -> str:
    """The shell snippet the emulator runs: tmux attach + interactive shell.

    The trailing ``exec bash -i`` keeps the window open after the user
    detaches from tmux (otherwise the window would snap shut and you'd
    lose terminal scrollback).
    """
    session = shlex.quote(config.tmux_session_name)
    return (
        f"tmux attach -t {session} \\; select-window -t {window_id} || true; "
        "exec bash -i"
    )


def _expand_linux_template(template: str, window_id: str) -> list[str]:
    """Substitute the ``{shell}`` placeholder and split into argv."""
    shell_cmd = _build_linux_shell_cmd(window_id)
    formatted = template.replace("{shell}", shlex.quote(shell_cmd))
    return shlex.split(formatted)


def _resolve_linux_template(user_id: int | None) -> str:
    """User setting > env var > empty (caller logs and skips)."""
    template = ""
    if user_id is not None:
        from .session import session_manager

        template = session_manager.get_user_settings(user_id).get(
            "local_terminal_cmd", ""
        )
    if not template:
        template = os.environ.get("CCBOT_LOCAL_TERMINAL_CMD", "")
    return template


async def _open_macos(window_id: str) -> None:
    tmux_cmd = _build_tmux_command(window_id)
    args = (
        _iterm_args(tmux_cmd)
        if await _is_iterm_running()
        else _terminal_app_args(tmux_cmd)
    )
    await _spawn(args)


async def _open_linux(window_id: str, user_id: int | None) -> None:
    template = _resolve_linux_template(user_id)
    if not template:
        logger.info(
            "local_terminal: Linux but no command configured "
            "(Settings → Local terminal, or CCBOT_LOCAL_TERMINAL_CMD env)"
        )
        return
    try:
        args = _expand_linux_template(template, window_id)
    except ValueError as e:
        logger.warning("local_terminal: bad template %r: %s", template, e)
        return
    if not args:
        logger.warning("local_terminal: empty argv from template %r", template)
        return
    if shutil.which(args[0]) is None:
        logger.warning(
            "local_terminal: %r not on PATH; reconfigure Settings → Local terminal",
            args[0],
        )
        return
    await _spawn(args)


async def _spawn(args: list[str]) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "local_terminal: spawn failed (rc=%d) for %r: %s",
                proc.returncode,
                args[0],
                stderr.decode(errors="replace")[:200],
            )
        else:
            logger.info("local_terminal: opened (%s)", args[0])
    except Exception as e:
        logger.warning("local_terminal: spawn raised: %s", e)


async def open_terminal_for_window(window_id: str, user_id: int | None = None) -> None:
    """Pop a native terminal attached to the given tmux window.

    Silent on unsupported platforms and on every spawn failure — this is
    a UX convenience, never load-bearing. ``user_id`` is required on
    Linux so we can resolve the user's selected template; macOS ignores
    it.
    """
    if not window_id:
        return
    system = platform.system()
    if system == "Darwin":
        await _open_macos(window_id)
    elif system == "Linux":
        await _open_linux(window_id, user_id)
    else:
        logger.debug("local_terminal: unsupported platform %s", system)
