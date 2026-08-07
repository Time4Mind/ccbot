"""Pure text helpers and filters for archived-session presentation.

The stateful archive facade imports these historical private names so existing
callers continue to use ``ccbot.handlers.archive`` unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..session import Session


__all__ = [
    "_RE_INJECTED_USER_MSG",
    "_RE_SYSTEM_UI_TEXT",
    "_shorten_workdir",
    "_clean_user_msg",
    "_truncate_at_word",
    "_display_name",
]

_RE_INJECTED_USER_MSG = re.compile(
    r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)"
)

_RE_SYSTEM_UI_TEXT = re.compile(
    r"^\s*(?:"
    r"\[[^\]\n]+\]\s*$"  # whole message is one bracketed marker
    r"|Set (?:model|effort|thinking) to\b"
    r"|Compact(?:ed|ing)\b"
    r"|Cleared\b"
    r"|Memory (?:updated|file)\b"
    r")",
    re.IGNORECASE,
)


def _shorten_workdir(path: str) -> str:
    """Replace the user's home prefix with ``~`` so paths fit on one row.
    Mirrors ``bot._common.shorten_workdir`` — kept here to avoid a
    handlers→bot import inversion."""
    if not path:
        return ""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def _clean_user_msg(text: str) -> str:
    """Collapse whitespace and strip a leading slash-command prefix.

    Doesn't truncate — the budget is handled at the accumulation level
    in ``_collect_user_messages``. The leading-slash strip means a row
    that starts with ``/resume real ask`` reads ``real ask`` (the
    user's actual ask, not the dispatch verb).
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if cleaned.startswith("/"):
        head, _, rest = cleaned.partition(" ")
        cleaned = rest if rest else head
    return cleaned.strip("` ")


def _truncate_at_word(text: str, budget: int) -> str:
    """Clip ``text`` to ``budget`` chars on the nearest whole-word
    boundary, appending ``…``.

    Scans back from the budget to the previous space; falls back to a
    hard cut only if no plausible word boundary exists in the last 24
    chars (very long URLs / single-word messages).
    """
    if len(text) <= budget:
        return text
    cut = text.rfind(" ", 0, budget)
    if cut < budget - 24:
        cut = budget
    return text[:cut].rstrip() + "…"


def _display_name(sess: Session) -> str:
    """Human-readable form of ``sess.name`` — Haiku produces kebab-case
    (``archive-pagination-fix``); for the body row and the inline
    button label we render it with spaces (``archive pagination fix``)
    so it reads as a natural phrase. Directory-derived names
    (``workdir-2``) pass through the same transform without harm.
    """
    return (sess.name or sess.id).replace("-", " ")
