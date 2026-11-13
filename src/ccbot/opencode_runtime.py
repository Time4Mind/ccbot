"""Recognize OpenCode's native composer without mistaking a shell for readiness."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


def _footer(pane: str) -> str:
    lines = [line.strip() for line in pane.splitlines() if line.strip()]
    if not lines:
        return ""
    # The mini UI anchors its composer on its last nonempty command bar.
    tail = lines[-1]
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
            if re.match(r"^┃\s+(?:Build|Plan)\s+·\s+", line):
                return line
    return ""


def is_ready(pane: str) -> bool:
    return (
        bool(_footer(pane))
        and status_line(pane) is None
        and interactive_panel(pane) is None
    )


def composer_identity(pane: str) -> tuple[str, str] | None:
    footer = _footer(pane)
    if not footer:
        return None
    model = re.match(r"^┃\s+(?:Build|Plan)\s+·\s+(.+?)\s+OpenAI\s*$", footer)
    if model:
        name = model[1]
        effort = re.search(r"\s+(low|medium|high|xhigh|max)\s*$", name, re.I)
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
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*Select model\s+esc\s*", line):
            for end in range(index + 1, len(lines)):
                if "View all providers ctrl+a" in lines[end]:
                    return "Settings", "\n".join(lines[index : end + 1])
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


def auth_state() -> AuthState:
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    try:
        providers = json.loads((data / "opencode/auth.json").read_text())
        auth = providers.get("openai") or {}
        # Report presence only. A successful inference proves current entitlement.
        return AuthState(auth.get("type") == "oauth" and bool(auth.get("refresh")))
    except (OSError, ValueError, AttributeError, TypeError):
        return AuthState(False)
