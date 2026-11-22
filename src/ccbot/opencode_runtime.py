"""Recognize OpenCode's native composer without mistaking a shell for readiness."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any


COMPOSER_PREFIX = r"^\s*┃\s+(?:Build|Plan)(?: auto)?\s+·\s+"


def _footer(pane: str) -> str:
    lines = [line.strip() for line in pane.splitlines() if line.strip()]
    if not lines:
        return ""
    # The mini UI anchors its composer on its last nonempty command bar.
    tail = lines[-1]
    # Narrow panes can wrap the project directory below the command bar.
    for index in range(max(0, len(lines) - 3), len(lines) - 1):
        command_bar = lines[index]
        if (
            "ctrl+p commands" in command_bar
            and (
                command_bar.startswith(("/", "~", "BUILD ", "PLAN "))
                or re.search(r"\besc\s+(?:again to\s+)?interrupt\b", command_bar)
            )
            and all(re.fullmatch(r"[\w./~ -]+", row) for row in lines[index + 1 :])
        ):
            tail = lines[index]
            break
    if re.match(r"^(?:BUILD|PLAN)\b", tail) and "ctrl+p" in tail:
        return tail
    if (
        re.match(r"^(?:/|~).+", tail)
        and ("ctrl+p commands" in tail or re.search(r"\s\d+\.\d+\.\d+\s*$", tail))
    ) or (
        "ctrl+p commands" in tail
        and re.search(r"\besc\s+(?:again to\s+)?interrupt\b", tail)
    ):
        for line in reversed(lines[-16:]):
            if re.match(COMPOSER_PREFIX, line):
                return line
    return ""


def composer_is_available(pane: str) -> bool:
    return bool(_footer(pane)) and interactive_panel(pane) is None


def is_ready(pane: str) -> bool:
    return composer_is_available(pane) and status_line(pane) is None


def composer_identity(pane: str) -> tuple[str, str] | None:
    footer = _footer(pane)
    if not footer:
        return None
    model = re.match(COMPOSER_PREFIX + r"(.+?)\s+OpenAI(?:\s+·\s+(.+?))?\s*$", footer)
    if model:
        if model[2]:
            return model[1], model[2]
        name = model[1]
        effort = re.search(
            r"\s+(none|minimal|low|medium|high|xhigh|max)\s*$", name, re.I
        )
        return (
            (name[: effort.start()].strip(), effort[1].lower())
            if effort
            else (name, "default")
        )
    match = re.search(
        r"\b(gpt-[\w.\-]+)\b(?:\s+(low|medium|high|xhigh|max))?", footer, re.I
    )
    return (match[1], match[2] or "default") if match else None


def status_line(pane: str) -> str | None:
    if not _footer(pane):
        return None
    tail = "\n".join(pane.splitlines()[-8:])
    if re.search(r"\besc\s+(?:(?:again\s+)?to\s+)?interrupt\b", tail, re.I):
        return "Working"
    return None


def interactive_panel(pane: str) -> tuple[str, str] | None:
    lines = pane.splitlines()
    nonempty = [line for line in lines if line.strip()]
    if nonempty and nonempty[-1].strip() == "┃":
        for start in range(len(lines) - 1, max(-1, len(lines) - 22), -1):
            if re.match(r"^\s*┃\s*△ Permission required\s*$", lines[start]):
                tail = "\n".join(lines[start:])
                if "Allow once" in tail and "Reject" in tail:
                    return "OpenCodePermission", "\n".join(
                        re.sub(r"^\s*┃\s?", "", line) for line in lines[start:]
                    ).strip()
        for end in range(len(lines) - 1, -1, -1):
            if re.match(
                r"^\s*┃\s*↑↓ select\s+enter submit\s+esc dismiss\s*$", lines[end]
            ):
                start = end
                while start > 0 and lines[start - 1].lstrip().startswith("┃"):
                    start -= 1
                content = "\n".join(
                    re.sub(r"^\s*┃\s?", "", line) for line in lines[start : end + 1]
                ).strip()
                if re.search(r"(?m)^\s*1\.\s+", content):
                    return "AskUserQuestion", content
    composer = _footer(pane)
    # In a new session the floating picker can cover the entire composer.
    # The native path/version bar still anchors the dialog to the live pane.
    version_bar = (
        nonempty[-1].strip()
        if nonempty and re.fullmatch(r"[~/].+\s{2,}\d+\.\d+\.\d+", nonempty[-1].strip())
        else ""
    )
    if not (composer or version_bar):
        return None
    for index, line in enumerate(lines):
        variant_title = re.search(r"\bSelect variant\s+esc\s*$", line)
        if variant_title is not None:
            # Old user/tool blocks can start with ┃ to the LEFT of this
            # floating dialog. They do not end the dialog: crop that margin
            # and stop at the visible composer or its path/version bar.
            footer = composer or version_bar
            end = next(
                (
                    i
                    for i in range(len(lines) - 1, index, -1)
                    if lines[i].strip() == footer
                ),
                len(lines),
            )
            left = max(0, variant_title.start() - 2)
            return "OpenCodeVariant", "\n".join(
                row[left:] for row in lines[index:end]
            ).strip()
        model_title = re.search(r"\bSelect model\s+esc\s*$", line)
        if model_title is not None:
            for end in range(index + 1, len(lines)):
                if re.search(
                    r"(?:View all providers|Connect provider) ctrl\+a", lines[end]
                ):
                    # Floating dialogs leave old TUI text to their left.
                    left = max(0, model_title.start() - 2)
                    return "Settings", "\n".join(
                        row[left:] for row in lines[index : end + 1]
                    )
    return None


def navigation_key(key: str, pane: str) -> str:
    return key


def navigation_keys(key: str, pane: str) -> tuple[str, ...]:
    if (
        key in ("Escape", "Esc")
        and status_line(pane)
        and interactive_panel(pane) is None
    ):
        return ("Escape", "Escape")
    return (key,)


async def wait_ready(
    capture: Callable[[], Awaitable[str]], *, timeout: float = 120.0
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if is_ready(await capture()):
            return True
        await asyncio.sleep(0.25)
    raise TimeoutError("OpenCode did not show its input composer")


@dataclass(frozen=True)
class AuthState:
    configured: bool
    provider: str = "openai"
    model: str = ""


def _openai_oauth() -> dict[str, Any]:
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    try:
        providers = json.loads((data / "opencode/auth.json").read_text())
        auth = providers.get("openai") or {}
        return auth if auth.get("type") == "oauth" and auth.get("refresh") else {}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def auth_state() -> AuthState:
    return AuthState(bool(_openai_oauth()))


def oauth_account_id() -> str | None:
    """Read the OpenAI subscription account associated with OpenCode."""
    account_id = _openai_oauth().get("accountId")
    return account_id if isinstance(account_id, str) and account_id else None
