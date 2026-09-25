"""Shared Codex startup state machine for leader and worker tmux panes."""

from __future__ import annotations

import asyncio
import os
import re
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
    HOOKS_REVIEW = "hooks_review"
    HOOKS_INLINE = "hooks_inline"
    MODEL_MIGRATION = "model_migration"
    RESUME_DIRECTORY = "resume_directory"
    UPDATE = "update"
    AUTHENTICATION = "authentication"
    UNHANDLED_MODAL = "unhandled_modal"
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
_UPDATE_TITLE = re.compile(r"^\s*update available\b", re.IGNORECASE)
_NUMBERED_OPTION = re.compile(r"^\s*(?:[›❯]\s*)?\d+\.\s*(.+?)\s*$")
_SELECTED_LINE = re.compile(r"^\s*[›❯]\s*(.*)$")


def _update_now_is_selected(text: str) -> bool:
    """Recognize the active update menu by actions, not version or chrome."""
    # tmux capture-pane includes the unused terminal rows below a short
    # startup modal. Trim those before taking the recent visible region.
    lines = text.rstrip().splitlines()[-16:]
    last_visible = next(
        (line.strip().casefold() for line in reversed(lines) if line.strip()), ""
    )
    if not re.search(r"\benter\b", last_visible):
        return False
    selected = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := _SELECTED_LINE.match(line))
    ]
    if not selected:
        return False
    selected_index, selected_text = selected[-1]
    choice = _NUMBERED_OPTION.match(selected_text)
    if choice is None or not re.match(r"update now\b", choice.group(1), re.I):
        return False
    heading = next(
        (
            index
            for index in range(selected_index, -1, -1)
            if _UPDATE_TITLE.match(lines[index])
        ),
        None,
    )
    if heading is None or selected_index - heading > 10:
        return False
    choices = [
        match.group(1).casefold()
        for line in lines[heading : heading + 12]
        if (match := _NUMBERED_OPTION.match(line))
    ]
    return any(choice.startswith("update now") for choice in choices) and (
        "skip" in choices
    )


def classify_codex_screen(text: str) -> CodexScreen:
    lower = text.lower()
    if (
        "do you trust the contents of this directory?" in lower
        and "1. yes, continue" in lower
    ):
        return CodexScreen.TRUST
    if (
        "hooks need review" in lower
        and "1. review hooks" in lower
        and "2. trust all and continue" in lower
        and "3. continue without trusting" in lower
    ):
        return CodexScreen.HOOKS_REVIEW
    if (
        "hooks need review before they can run" in lower
        and "press t to trust all" in lower
        and "enter to review hooks" in lower
        and "esc to close" in lower
    ):
        return CodexScreen.HOOKS_INLINE
    if (
        "choose how you'd like codex to proceed" in lower
        and "1. try new model" in lower
        and "2. use existing model" in lower
        and "press enter to confirm" in lower
    ):
        return CodexScreen.MODEL_MIGRATION
    if (
        "choose working directory to resume this session" in lower
        and "1. use session directory" in lower
        and "2. use current directory" in lower
        and "press enter to continue" in lower
    ):
        return CodexScreen.RESUME_DIRECTORY
    if _update_now_is_selected(text):
        return CodexScreen.UPDATE
    if any(
        marker in lower
        for marker in (
            "sign in with chatgpt",
            "sign in with device code",
            "provide your own api key",
        )
    ):
        return CodexScreen.AUTHENTICATION
    if is_codex_ready(text):
        return CodexScreen.READY
    if len(re.findall(r"(?m)^\s*(?:›\s*)?\d+\.\s+", text)) >= 2:
        return CodexScreen.UNHANDLED_MODAL
    return CodexScreen.WAITING


def is_codex_ready(text: str) -> bool:
    """Return true only for Codex's real input box, never a modal cursor."""
    if not text:
        return False
    lines = text.strip().splitlines()
    prompt_markers = [
        index for index, line in enumerate(lines) if line.lstrip().startswith("›")
    ]
    if not prompt_markers:
        return False
    latest_prompt = prompt_markers[-1]
    if _NUMBERED_OPTION.match(lines[latest_prompt]):
        return False
    title = next(
        (
            index
            for index in range(latest_prompt, -1, -1)
            if lines[index].strip().lower().startswith("openai codex")
        ),
        max(0, latest_prompt - 8),
    )
    current = "\n".join(lines[title:])
    if parse_status_line(current) is not None or is_interactive_ui(current):
        return False
    lower = current.lower()
    if any(
        marker in lower
        for marker in (
            "do you trust the contents of this directory?",
            "choose working directory to resume this session",
            "press enter to continue",
            "sign in with chatgpt",
            "sign in with device code",
            "provide your own api key",
            "update available",
            "hooks need review before they can run",
            "press t to trust all",
            "choose how you'd like codex to proceed",
            "use existing model",
        )
    ):
        return False
    if latest_prompt < len(lines) - 6:
        return False
    return (
        "openai codex" in current.lower()
        or parse_codex_model_effort(current) is not None
    )


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
    once. After updating, Codex may either return directly to its composer or
    exit to the shell. In the latter case, the exact original command is
    relaunched. A second update prompt is a bounded error.
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
            if _is_shell(process):
                await relaunch(command)
                waiting_for_updater_exit = False
                relaunched_after_update = True
                ready_since = None
            elif classify_codex_screen(text) is CodexScreen.READY:
                now = loop.time()
                if ready_since is None:
                    ready_since = now
                if now - ready_since >= settle_time:
                    return CodexStartupResult(updated=True)
            else:
                ready_since = None
            await asyncio.sleep(max(0.0, poll_interval))
            continue

        # A finished updater can leave its old menu in tmux scrollback while
        # the shell is waiting for a relaunch. It is no longer an active modal.
        if _is_shell(process):
            ready_since = None
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
        elif screen is CodexScreen.HOOKS_REVIEW:
            await send_key("DOWN")
            await send_key("ENTER")
        elif screen is CodexScreen.HOOKS_INLINE:
            await send_key("t")
        elif screen is CodexScreen.MODEL_MIGRATION:
            await send_key("DOWN")
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
        elif screen is CodexScreen.AUTHENTICATION:
            raise CodexStartupError("Codex requires authentication")
        elif screen is CodexScreen.UNHANDLED_MODAL:
            raise CodexStartupError("Codex showed an unsupported startup prompt")

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
