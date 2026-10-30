"""Recognize ZCode's live composer and credential configuration without model calls."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import re

_FOOTER = re.compile(r"^\s*│\s*(\S+)\s+(\S+)\s*\|\s*(\S+)\s*│\s*$")
_FRAME = re.compile(r"^\s*╭─\s*(Yolo|Plan|Guarded|Auto|Normal)\s*─", re.I)
_STATUS_SUFFIX = re.compile(
    r"^\s*(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+esc to interrupt)?\s*(?:\d+(?:\.\d+)?[KMG]?\s*\(\d+(?:\.\d+)?%\))?\s*$"
)


def composer_identity(pane: str) -> tuple[str, str] | None:
    lines = [line for line in pane.splitlines() if line.strip()]
    while lines and _STATUS_SUFFIX.fullmatch(lines[-1]):
        lines.pop()
    if not lines or not lines[-1].lstrip().startswith("╰─"):
        return None
    tail = lines[-14:]
    if not any(_FRAME.match(line) for line in tail):
        return None
    for line in reversed(tail):
        match = _FOOTER.fullmatch(line)
        if match:
            return match[1], match[3]
    return None


def status_line(pane: str) -> str | None:
    if composer_identity(pane) is None:
        return None
    for line in pane.splitlines()[-12:]:
        match = re.search(r"([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏])\s+esc to interrupt", line)
        if match:
            return match[1] + " Working"
    return None


def is_ready(pane: str) -> bool:
    return composer_identity(pane) is not None and "Type a prompt" in "\n".join(
        pane.splitlines()[-8:]
    )


def interactive_panel(pane: str) -> tuple[str, str] | None:
    """Require a complete native panel beside the actual ZCode composer."""
    lines = pane.splitlines()
    if composer_identity(pane) is None:
        return None
    top = re.compile(
        r"^\s*[╭┌]─+\s*(Models|Effort|Modes|Question \d+/\d+:.*|Review answers)\s*─"
    )
    for index in range(len(lines) - 1, max(-1, len(lines) - 40), -1):
        match = top.match(lines[index])
        if match is None:
            continue
        for end in range(index + 1, len(lines)):
            if re.match(r"^\s*[╰└]─+[╯┘]\s*$", lines[end]):
                title = match[1]
                name = (
                    "AskUserQuestion"
                    if title.startswith(("Question ", "Review answers"))
                    else "Settings"
                )
                return name, "\n".join(lines[index : end + 1])
    return None


def navigation_key(key: str, pane: str) -> str:
    """Cancel a picker draft as well as its native autocomplete overlay."""
    if key not in ("Escape", "Esc"):
        return key
    panel = interactive_panel(pane)
    if (
        panel
        and panel[0] == "Settings"
        and re.search(r"(?m)^\s*│\s*/(?:model|effort|mode)(?:\s[^│]*)?\s*│\s*$", pane)
    ):
        return "C-u"
    return key


async def wait_ready(
    capture: Callable[[], Awaitable[str]], *, timeout: float = 120.0
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if is_ready(await capture()):
            return True
        await asyncio.sleep(0.25)
    raise TimeoutError("ZCode did not show its input composer")


@dataclass(frozen=True)
class AuthState:
    configured: bool
    provider: str = ""
    model: str = ""


def provider_file() -> Path:
    explicit = os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.environ.get("ZCODE_DATA_BASE_DIR", str(Path.home()))).expanduser()
    return base / ".zcode/v2/provider_config.json"


def auth_state() -> AuthState:
    try:
        document = json.loads(provider_file().read_text())
        if document.get("schemaVersion") != 1:
            return AuthState(False)
        settings = document["config"]
        selection = settings.get("defaultModelSelection") or {}
        provider = selection.get("providerId", "")
        model = selection.get("modelId", "")
        for rule in settings.get("providerConfigRules", {}).get("providerRules", []):
            if rule.get("providerId") != provider:
                continue
            config = rule.get("config") or {}
            access = config.get("access") or {}
            key_ready = access.get("type") in (
                "api-key",
                "zhipu-coding-plan-api-key",
            ) and bool(str(access.get("apiKey") or "").strip())
            endpoint_ready = bool((config.get("api") or {}).get("baseUrl"))
            return AuthState(
                bool(provider and model and key_ready and endpoint_ready),
                provider,
                model,
            )
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        pass
    return AuthState(False)
