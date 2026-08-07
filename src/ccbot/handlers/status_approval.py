"""Pure parsing helpers for interactive auto-approval."""

from __future__ import annotations

import re
from typing import Protocol


class InteractiveContent(Protocol):
    """Subset of terminal-parser content used to build a signature."""

    name: str
    content: str


_OPTION_LINE_RE = re.compile(r"^[\s❯›>]*?(\d+)\.\s+(.+?)\s*$")
_DIGIT_RUN_RE = re.compile(r"\d+")
_DURABLE_YES_RE = re.compile(
    r"during this session|don'?t ask again|allow all|always allow|for the rest of",
    re.IGNORECASE,
)


def auto_approve_progress(pane_text: str) -> str:
    """Return pane content with volatile numeric counters normalized."""
    return _DIGIT_RUN_RE.sub("#", pane_text)


def parse_best_yes_option(pane_text: str) -> str | None:
    """Prefer a durable Yes menu option, falling back to the first Yes."""
    first_yes: str | None = None
    for raw in pane_text.splitlines():
        match = _OPTION_LINE_RE.match(raw)
        if not match:
            continue
        number, label = match.group(1), match.group(2)
        if not label.lower().startswith("yes"):
            continue
        if first_yes is None:
            first_yes = number
        if _DURABLE_YES_RE.search(label):
            return number
    return first_yes


def auto_approve_signature(pane_text: str, content: InteractiveContent | None) -> str:
    """Return a stable identity for one on-screen approval prompt."""
    if content is not None:
        return f"{content.name}\x1f{content.content}"
    options = [
        f"{match.group(1)}.{match.group(2)}"
        for line in pane_text.splitlines()
        if (match := _OPTION_LINE_RE.match(line))
    ]
    return "\n".join(options)
