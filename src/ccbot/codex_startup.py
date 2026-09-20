"""Shared Codex startup state machine for leader and worker tmux panes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from .terminal_parser import (
    is_interactive_ui,
    parse_codex_model_effort,
    parse_status_line,
)


class CodexStartupError(RuntimeError):
    """Codex did not reach a usable input state."""


class CodexScreen(str, Enum):
    WAITING = "waiting"
    TRUST = "trust"
    RESUME_DIRECTORY = "resume_directory"
    UPDATE = "update"
    READY = "ready"


@dataclass(frozen=True)
class CodexStartupResult:
    updated: bool = False


_SHELL_PROCESSES = {
    "ash",
    "bash",
    "dash",
    "fish",
    "ksh",
    "nu",
    "pwsh",
    "sh",
    "tcsh",
    "zsh",
}
CODEX_READY_SETTLE_SECONDS = 3.0


def classify_codex_screen(text: str) -> CodexScreen:
    lower = text.lower()
    if (
        "do you trust the contents of this directory?" in lower
        and "1. yes, continue" in lower
    ):
        return CodexScreen.TRUST
    if (
        "choose working directory to resume this session" in lower
        and "1. use session directory" in lower
        and "2. use current directory" in lower
        and "press enter to continue" in lower
    ):
        return CodexScreen.RESUME_DIRECTORY
    if (
        "update available!" in lower
        and "1. update now" in lower
        and "2. skip" in lower
        and "press enter to continue" in lower
    ):
        return CodexScreen.UPDATE
    if is_codex_ready(text):
        return CodexScreen.READY
    return CodexScreen.WAITING


def is_codex_ready(text: str) -> bool:
    """Return true only for Codex's real input box, never a modal cursor."""
    if not text or parse_status_line(text) is not None or is_interactive_ui(text):
        return False
    lower = text.lower()
    if any(
        marker in lower
        for marker in (
            "do you trust the contents of this directory?",
            "choose working directory to resume this session",
            "press enter to continue",
            "sign in with chatgpt",
            "sign in with device code",
            "provide your own api key",
            "update available!",
        )
    ):
        return False
    lines = text.strip().splitlines()
    if not any(line.lstrip().startswith("›") for line in lines[-6:]):
        return False
    return "openai codex" in lower or parse_codex_model_effort(text) is not None


def _is_shell(process: str) -> bool:
    return os.path.basename(process.strip()).lower() in _SHELL_PROCESSES


async def drive_codex_startup(
    *,
    command: str,
    capture: Callable[[], Awaitable[str]],
    current_process: Callable[[], Awaitable[str]],
    send_key: Callable[[str], Awaitable[None]],
    relaunch: Callable[[str], Awaitable[None]],
    timeout: float,
    poll_interval: float = 0.25,
    ready_settle_time: float = CODEX_READY_SETTLE_SECONDS,
) -> CodexStartupResult:
    """Drive known startup prompts until the real input state is visible.

    A composer must remain continuously ready for ``ready_settle_time`` so a
    delayed startup modal can still be handled. An offered update is accepted
    once. Codex's updater exits instead of returning to the TUI, so the exact
    original command is relaunched after the pane returns to its shell. A
    second update prompt is a bounded error.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    updated = False
    waiting_for_updater_exit = False
    relaunched_after_update = False
    ready_since: float | None = None
    settle_time = max(0.0, ready_settle_time)

    while loop.time() <= deadline:
        text, process = await asyncio.gather(capture(), current_process())

        if waiting_for_updater_exit:
            ready_since = None
            if _is_shell(process):
                await relaunch(command)
                waiting_for_updater_exit = False
                relaunched_after_update = True
            await asyncio.sleep(max(0.0, poll_interval))
            continue

        screen = classify_codex_screen(text)
        if screen is CodexScreen.READY:
            now = loop.time()
            if ready_since is None:
                ready_since = now
            if now - ready_since >= settle_time:
                return CodexStartupResult(updated=updated)
        else:
            ready_since = None
        if screen is CodexScreen.TRUST:
            await send_key("ENTER")
        elif screen is CodexScreen.RESUME_DIRECTORY:
            await send_key("DOWN")
            await send_key("ENTER")
        elif screen is CodexScreen.UPDATE:
            if relaunched_after_update or updated:
                raise CodexStartupError("Codex repeated update prompt after relaunch")
            await send_key("ENTER")
            updated = True
            waiting_for_updater_exit = True

        await asyncio.sleep(max(0.0, poll_interval))

    if waiting_for_updater_exit:
        raise CodexStartupError("Codex update did not finish before startup timeout")
    raise CodexStartupError("Codex did not become ready before startup timeout")


__all__ = [
    "CODEX_READY_SETTLE_SECONDS",
    "CodexScreen",
    "CodexStartupError",
    "CodexStartupResult",
    "classify_codex_screen",
    "drive_codex_startup",
    "is_codex_ready",
]
