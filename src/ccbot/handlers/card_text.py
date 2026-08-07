"""Parse and sanitize transcript text before it becomes a card event."""

from __future__ import annotations

import re
import time

__all__ = [
    "_trim",
    "_EXPQUOTE_BLOCK_RE",
    "_EXPQUOTE_ANY_RE",
    "_EXPQUOTE_INNER_RE",
    "_extract_expquote_inner",
    "_strip_for_card",
    "_parse_timestamp",
    "_TOOL_HEAD_RE",
    "_split_tool_text",
]


def _trim(s: str, limit: int = 200) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


_EXPQUOTE_BLOCK_RE = re.compile(
    r"\x02EXPQUOTE_START\x02.*?\x02EXPQUOTE_END\x02",
    re.DOTALL,
)
# Drop residual EXPQUOTE_START / EXPQUOTE_END sentinels that didn't
# pair up (transcript_format builds tool blocks with nested sentinels;
# the outer pair gets stripped but the inner one can leak in body).
_EXPQUOTE_ANY_RE = re.compile(r"\x02EXPQUOTE_(?:START|END)\x02")
# Pull the inner content out of an EXPQUOTE_START / END pair.
_EXPQUOTE_INNER_RE = re.compile(
    r"\x02EXPQUOTE_START\x02(.*?)\x02EXPQUOTE_END\x02",
    re.DOTALL,
)


def _extract_expquote_inner(text: str) -> str:
    """Return the content between the FIRST EXPQUOTE_START / END pair."""
    m = _EXPQUOTE_INNER_RE.search(text or "")
    return m.group(1) if m else ""


def _strip_for_card(text: str) -> str:
    """Strip residue that would render literally in MarkdownV2 mode.

    Card text is now sent with ``parse_mode=MarkdownV2`` (via
    ``send_with_fallback`` / ``_send_card_md``), so MarkdownV2 markers
    like ``**bold**`` get rendered properly. We only strip:

    * The full ``EXPQUOTE_START … EXPQUOTE_END`` block when it appears
      INSIDE a head line (heads are one-liners; the embedded quote
      belongs in the body, not the head).
    * Any orphan ``EXPQUOTE_*`` sentinel that escaped pair-matching.
    * ``$HOME`` → ``~`` so long Mac paths don't waste 30+ chars.

    The MarkdownV2 ``convert_markdown`` step inside ``send_with_fallback``
    handles escaping special chars and expanding paired EXPQUOTE blocks
    into expandable blockquote syntax.
    """
    import os

    out = _EXPQUOTE_BLOCK_RE.sub("", text)
    out = _EXPQUOTE_ANY_RE.sub("", out)
    home = os.path.expanduser("~")
    if home and home != "/":
        out = out.replace(home, "~")
    return out


def _parse_timestamp(ts: str) -> float:
    """Parse ISO-8601 timestamp from a JSONL entry into epoch seconds.

    Returns ``time.time()`` when the input is empty or unparseable so
    callers can use the result unconditionally as an ``started_at``.
    """
    if not ts:
        return time.time()
    try:
        import datetime as _dt

        # Tolerate trailing Z + offset forms; fromisoformat handles "+HH:MM"
        # natively but historically chokes on "Z".
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


_TOOL_HEAD_RE = re.compile(
    r"^\s*\**(?P<name>[A-Za-z][\w-]*)\**\s*\((?P<args>.*)\)\s*$", re.DOTALL
)


def _split_tool_text(raw: str) -> tuple[str, str, str, str]:
    """Split transcript_format's tool text into name / args / summary / content.

    ``raw`` reaches us in the shape::

        **ToolName**(args)            ← head_block (can span multiple
                                        lines if args = bash heredoc /
                                        multi-line edit diff)
          ⎿  Output N lines           ← summary line (optional)
        \\x02EXPQUOTE_START\\x02<content>\\x02EXPQUOTE_END\\x02

    The summary line starts with whitespace + ``⎿``. Everything before
    that marker (or before the EXPQUOTE_START sentinel, whichever comes
    first) is the head_block — possibly multiple lines when args
    contains literal newlines (bash heredoc).

    Returns ``(name, args, summary, content)`` where:

    * ``name``    = bare tool name (``Bash``, ``Read``, ``Edit``).
    * ``args``    = whatever was between the outermost parens — pushed
      under the spoiler so long commands don't blow up the head line.
    * ``summary`` = ``⎿`` line content (``Output 5 lines``).
    * ``content`` = inside the EXPQUOTE block, minus duplicate head /
      summary lines that transcript_parser sometimes re-embeds.

    When the head doesn't parse as ``Name(args)`` (orphan tool_result
    fallback or weird format), the full head_block lands in ``name``
    and ``args`` is empty.
    """
    if not raw:
        return "", "", "", ""

    # Locate end-of-head: the first ``\n  ⎿`` summary marker OR the
    # first ``\x02EXPQUOTE_START\x02`` content marker, whichever comes
    # earlier. Whatever's BEFORE that boundary is the head_block (may
    # span multiple lines when args is a bash heredoc).
    summary_marker_re = re.compile(r"\n\s*⎿")
    summary_match = summary_marker_re.search(raw)
    quote_idx = raw.find("\x02EXPQUOTE_START\x02")
    head_end = len(raw)
    if summary_match is not None:
        head_end = min(head_end, summary_match.start())
    if quote_idx >= 0:
        head_end = min(head_end, quote_idx)
    head_block = raw[:head_end].rstrip("\n")

    name = _strip_for_card(head_block)
    args = ""
    m = _TOOL_HEAD_RE.match(head_block)
    if m:
        name = m.group("name").strip()
        args = m.group("args").strip()
        # The first-line ``Name(`` prefix being matched means the regex
        # already used DOTALL — args may legitimately contain newlines.

    summary = ""
    after_head = raw[head_end:]
    if after_head.startswith("\n"):
        after_head = after_head[1:]
    # Pull the summary line if it's first.
    if after_head.lstrip(" ").startswith("⎿"):
        nl = after_head.find("\n")
        if nl == -1:
            summary_line = after_head
            after_head = ""
        else:
            summary_line = after_head[:nl]
            after_head = after_head[nl + 1 :]
        summary = _strip_for_card(summary_line.lstrip(" ").lstrip("⎿").strip())

    # ``after_head`` is now either an EXPQUOTE block or plain rest.
    inner = _extract_expquote_inner(after_head) if after_head else ""
    content = inner if inner else after_head
    # Drop duplicate head/summary rows that transcript_parser may
    # re-embed at the top of the EXPQUOTE block.
    if content:
        content_lines = content.split("\n")
        first_norm = _strip_for_card(content_lines[0]).strip()
        head_norm = _strip_for_card(head_block).strip()
        if (
            first_norm == head_norm
            or (head_norm and first_norm.endswith(head_norm))
            or (
                first_norm.startswith(("✓ ", "▷ ", "✗ "))
                and head_norm
                and head_norm in first_norm
            )
        ):
            content_lines = content_lines[1:]
        if content_lines and content_lines[0].lstrip().startswith("⎿"):
            content_lines = content_lines[1:]
        content = "\n".join(content_lines).strip("\n")
    return name, args, summary, content
