"""Pure text helpers and filters for archived-session presentation.

The stateful archive facade imports these historical private names so existing
callers continue to use ``ccbot.handlers.archive`` unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from ..session import Session


__all__ = [
    "_RE_INJECTED_USER_MSG",
    "_RE_SYSTEM_UI_TEXT",
    "_shorten_workdir",
    "_clean_user_msg",
    "_truncate_at_word",
    "_shorten_links",
    "_fit_archive_description",
    "_display_name",
]

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

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


def _shorten_links(text: str, user_id: int) -> str:
    """Replace full URLs with compact labels that retain their destination."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        url = raw.rstrip(".,;:!?)]}")
        suffix = raw[len(url) :]
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        known = (
            (("yql.yandex-team.ru", "yql.yandex.ru"), "archive.link.yql"),
            (("st.yandex-team.ru", "tracker.yandex.ru"), "archive.link.tracker"),
            (("github.com",), "archive.link.github"),
            (("arcanum.yandex-team.ru",), "archive.link.arcanum"),
            (("wiki.yandex-team.ru",), "archive.link.wiki"),
        )
        from ..i18n import t

        for hosts, key in known:
            if host in hosts:
                return t(user_id, key) + suffix
        return t(user_id, "archive.link.generic", host=host or "link") + suffix

    return _URL_RE.sub(replace, text)


def _fit_archive_description(
    messages: list[str], user_id: int, budget: int = 70
) -> str:
    """Render at most two dot-prefixed prompts within one visible-char budget."""
    cleaned = [
        _shorten_links(message.strip(), user_id)
        for message in messages
        if message.strip()
    ][:2]
    if not cleaned or budget <= 0:
        return ""
    content_budget = max(0, budget - 2 * len(cleaned))
    caps = [content_budget // len(cleaned)] * len(cleaned)
    for index in range(content_budget % len(cleaned)):
        caps[index] += 1

    # A short prompt donates its unused share to the other prompt.
    spare = sum(max(0, cap - len(message)) for cap, message in zip(caps, cleaned))
    caps = [min(cap, len(message)) for cap, message in zip(caps, cleaned)]
    while spare:
        recipients = [i for i, message in enumerate(cleaned) if caps[i] < len(message)]
        if not recipients:
            break
        for index in recipients:
            if not spare:
                break
            caps[index] += 1
            spare -= 1

    def clip(message: str, cap: int) -> str:
        if len(message) <= cap:
            return message
        if cap <= 1:
            return "…"[:cap]
        return _truncate_at_word(message, cap - 1)

    return "<br>".join(f"· {clip(message, cap)}" for message, cap in zip(cleaned, caps))


def _display_name(sess: Session) -> str:
    """Human-readable form of ``sess.name`` — Haiku produces kebab-case
    (``archive-pagination-fix``); for the body row and the inline
    button label we render it with spaces (``archive pagination fix``)
    so it reads as a natural phrase. Directory-derived names
    (``workdir-2``) pass through the same transform without harm.
    """
    return (sess.name or sess.id).replace("-", " ")
