"""Low-level card text sizing, chunking, and elapsed-time helpers."""

from __future__ import annotations

import re

from .card_text import _strip_for_card
from .card_types import (
    CARD_PAGE_BUDGET,
    CARD_PAGE_LINES_DEFAULT,
    CARD_PAGE_LINES_OVERSHOOT,
    SPOILER_MAX_LINES,
    Event,
)

__all__ = [
    "_format_elapsed",
    "_format_hhmm",
    "_format_hhmmss",
    "_is_in_flight",
    "_body_trim",
    "_SENTENCE_END_RE",
    "_table_continuation_prefix",
    "_chunk_final_text",
    "_trimmed_body",
    "_count_lines",
    "_MD_V2_ESCAPE_CHARS",
    "_estimate_md_v2_size",
    "_char_pos_at_byte_budget",
]

# ─── Render helpers ───────────────────────────────────────────────────


def _format_elapsed(seconds: float) -> str:
    """Format ``M:SS`` for an elapsed timer (negative → ``0:00``)."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _format_hhmm(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M")


def _format_hhmmss(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _is_in_flight(event: Event, events: list[Event], idx: int) -> bool:
    """Per spec: ``⏳`` lives only on the LAST event of the latest page.

    Older events with ``completed_at=None`` are implicitly considered
    finished by virtue of a newer event having started — for the user
    the timer "moves" with whatever block claude is currently producing.
    """
    if idx != len(events) - 1:
        return False
    if event.completed_at is not None:
        return False
    return event.type in ("tool_use", "thinking", "text")


def _body_trim(body: str, max_lines: int = SPOILER_MAX_LINES) -> str:
    """Trim body content to ``max_lines`` lines. Excess → ``… (+N more lines)``."""
    if not body:
        return ""
    lines = body.split("\n")
    if len(lines) <= max_lines:
        return body
    kept = lines[:max_lines]
    extra = len(lines) - max_lines
    kept.append(f"… (+{extra} more lines)")
    return "\n".join(kept)


_SENTENCE_END_RE = re.compile(r"[.!?][\s)\]\"»]*\s")


def _table_continuation_prefix(chunk: str, remaining: str) -> str:
    """Header+separator rows to prepend when a cut landed inside a GFM table.

    If ``chunk`` ends with table rows (a trailing run of ``|``-lines whose
    first two are a header + ``|---|`` separator) and ``remaining`` starts
    with another table row, the table was split mid-body — the continuation
    page would render as headerless junk. Returns ``"header\\nsep\\n"`` so
    the caller can re-emit them; ``""`` when no table was cut.
    """
    rem_first = remaining.lstrip("\n").split("\n", 1)[0]
    if not rem_first.lstrip().startswith("|"):
        return ""
    lines = chunk.rstrip("\n").split("\n")
    i = len(lines)
    while i > 0 and lines[i - 1].lstrip().startswith("|"):
        i -= 1
    run = lines[i:]
    if len(run) < 2:
        return ""
    sep_core = run[1].strip().strip("|").replace("|", "").replace(" ", "")
    if not sep_core or not set(sep_core) <= {"-", ":"}:
        return ""
    return run[0] + "\n" + run[1] + "\n"


def _chunk_final_text(
    text: str,
    budget_lines: int = CARD_PAGE_LINES_DEFAULT,
    byte_budget: int = CARD_PAGE_BUDGET,
) -> list[str]:
    """Split a long final answer into chunks ≤ ``budget_lines`` AND ≤ ``byte_budget``.

    Smart-boundary preference (per user spec): paragraph (``\\n\\n``) →
    line (``\\n``) → sentence terminator (``.!?``) → word (space) → hard.
    Allows up to ``CARD_PAGE_LINES_OVERSHOOT`` extra lines so a sentence
    isn't broken mid-content. NEVER breaks mid-word.

    The byte cap mirrors Telegram's 4096-byte edit limit (with headroom
    for header / divider / footer / bg-panel). Without it, a wide
    single-paragraph answer can pass the line cap and still overflow
    after MarkdownV2 escaping — every reserved char gets a ``\\``
    prefix, blowing the rendered size past the limit.

    Empty / short input returns a single-chunk list.
    """
    if not text:
        return []
    if _count_lines(text) <= budget_lines and _estimate_md_v2_size(text) <= byte_budget:
        return [text]

    chunks: list[str] = []
    remaining = text
    while (
        _count_lines(remaining) > budget_lines
        or _estimate_md_v2_size(remaining) > byte_budget
    ):
        rem_lines = remaining.split("\n")
        # 1. Paragraph break — look at the last \n\n within budget+overshoot,
        #    clamped to the byte budget so we never search past the safe
        #    rendered-size window.
        cap = budget_lines + CARD_PAGE_LINES_OVERSHOOT
        char_cap_lines = sum(
            len(rem_lines[i]) + 1 for i in range(min(cap, len(rem_lines)))
        )
        char_cap_bytes = _char_pos_at_byte_budget(remaining, byte_budget)
        char_cap = (
            min(char_cap_lines, char_cap_bytes) if char_cap_bytes else char_cap_lines
        )
        # If even one char is over byte budget, char_cap_bytes is 0 — use a
        # minimal cap so the boundary scans still see SOMETHING. Edge case.
        if char_cap <= 0:
            char_cap = max(1, char_cap_lines)
        cut = remaining.rfind("\n\n", 0, char_cap)

        # 2. Line break within budget (no overshoot).
        if cut <= 0:
            char_budget = sum(
                len(rem_lines[i]) + 1 for i in range(min(budget_lines, len(rem_lines)))
            )
            char_budget = min(char_budget, char_cap)
            cut = remaining.rfind("\n", 0, char_budget)

        # 3. Sentence terminator within budget+overshoot.
        if cut <= 0:
            m_iter = list(_SENTENCE_END_RE.finditer(remaining[:char_cap]))
            if m_iter:
                cut = m_iter[-1].end()

        # 4. Word boundary within budget+overshoot.
        if cut <= 0:
            cut = remaining.rfind(" ", 0, char_cap)

        # 5. Hard cut (last resort — only if no other boundary found in
        #    the entire overshoot window). Use char_cap to avoid mid-word
        #    if possible; otherwise raw budget cut.
        if cut <= 0:
            cut = char_cap if char_cap > 0 else len(remaining)

        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n").lstrip(" ")
        # Cut landed inside a GFM table → re-emit header+separator so the
        # next page renders as a valid table. Length guard keeps forward
        # progress (never prepend more than the cut consumed).
        if chunk:
            prefix = _table_continuation_prefix(chunk, remaining)
            if prefix and len(prefix) < cut:
                remaining = prefix + remaining
    if remaining:
        chunks.append(remaining)
    return chunks


def _trimmed_body(body: str) -> str:
    """Trim and home-path-clean a tool / thinking body so it's safe to
    drop into a spoiler block. Returns empty when there's nothing left
    to show."""
    return _body_trim(_strip_for_card(body))


def _count_lines(text: str) -> int:
    """Count logical \\n-delimited lines in a rendered string."""
    if not text:
        return 0
    return text.count("\n") + 1


# Telegram MarkdownV2 reserved chars — each one gains a leading ``\``
# during ``convert_markdown``. We use this as an upper-bound estimate of
# the post-render byte count without paying for a real telegramify
# round-trip on every event. The bound is sloppy on purpose: better to
# oversplit a long answer than to send a 4096+ byte payload and lose the
# whole card edit to ``Message_too_long``.
_MD_V2_ESCAPE_CHARS = frozenset("_*[]()~`>#+-=|{}.!\\")


def _estimate_md_v2_size(text: str) -> int:
    """Upper bound on ``len(convert_markdown(text))`` (chars / bytes-ASCII).

    Each MarkdownV2 reserved char contributes ``+1`` over the raw length
    for its escape backslash. Real telegramify-markdown sometimes leaves
    a few of these unescaped inside valid markdown tokens (``**bold**``
    etc.), but using an over-estimate is the safe direction — we'd
    rather chunk earlier than discover overflow at edit time.
    """
    if not text:
        return 0
    extra = sum(1 for c in text if c in _MD_V2_ESCAPE_CHARS)
    return len(text) + extra


def _char_pos_at_byte_budget(text: str, byte_budget: int) -> int:
    """Largest ``p`` such that ``_estimate_md_v2_size(text[:p]) <= byte_budget``.

    Returns ``len(text)`` if the whole string fits. Used by
    ``_chunk_final_text`` to clamp the boundary-search window when a
    long answer would otherwise overflow Telegram's 4096-byte edit cap
    even at very few visual lines.
    """
    if byte_budget <= 0 or not text:
        return 0
    size = 0
    for i, c in enumerate(text):
        bump = 2 if c in _MD_V2_ESCAPE_CHARS else 1
        if size + bump > byte_budget:
            return i
        size += bump
    return len(text)
