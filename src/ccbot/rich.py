"""Bot API 10.1 rich-message calls (sendRichMessage / rich editMessageText).

PTB 22.x wraps Bot API 10.0, so rich messages go through the raw
``Bot._post`` escape hatch until PTB ships native support; ``ExtBot``
overrides ``_do_post``, so these calls still pass through the
application-level ``AIORateLimiter``.

Core responsibilities:
  - to_rich_markdown: adapt our internal markdown for the Rich Markdown
    parser — expandable-quote sentinels become <details> blocks, bare
    ``<`` outside code spans is escaped to ``&lt;`` (the parser silently
    swallows anything that looks like an unsupported HTML tag), and table
    cells are wrapped in <sub> so native tables render in a smaller font
    (the API exposes no font-size control; clients draw sub/sup smaller).
  - send_rich_message / edit_rich_message: thin raw-API wrappers returning
    PTB ``Message`` objects.

Key functions: to_rich_markdown, send_rich_message, edit_rich_message.
"""

import re
from typing import Any, cast

from telegram import InlineKeyboardMarkup, Message
from telegram.ext import ExtBot

from .transcript_format import EXPANDABLE_QUOTE_END, EXPANDABLE_QUOTE_START

# Rich messages cap (Bot API 10.1): 32768 UTF-8 chars of text.
RICH_MAX_CHARS = 32768

# Fenced code blocks (tolerating an unterminated fence at EOF) and inline
# code spans — `<` inside these is preserved verbatim by the rich parser.
_CODE_SPAN_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`")

# HTML tags the Rich Markdown parser supports (see "Rich HTML style" in the
# Bot API docs). A `<` starting one of these is left alone; any other `<`
# is escaped, because the parser drops unknown tag-shaped fragments
# silently (``x<y>z`` renders as ``xz``).
_ALLOWED_TAG_RE = re.compile(
    r"</?(?:"
    r"b|strong|i|em|u|ins|s|strike|del|code|pre|mark|sub|sup"
    r"|tg-spoiler|tg-emoji|tg-time|tg-math|tg-math-block"
    r"|tg-collage|tg-slideshow|tg-map|tg-reference"
    r"|a|img|video|audio|figure|figcaption|cite|aside"
    r"|details|summary|blockquote|footer"
    r"|h[1-6]|p|ul|ol|li|table|tr|th|td|caption|br|hr"
    r")(?=[\s/>])[^<>]*>",
    re.IGNORECASE,
)

_EXPQUOTE_RE = re.compile(
    re.escape(EXPANDABLE_QUOTE_START) + r"([\s\S]*?)" + re.escape(EXPANDABLE_QUOTE_END)
)

_SUMMARY_MAX = 64


def _escape_lt(segment: str) -> str:
    """Escape ``<`` to ``&lt;`` unless it starts a supported HTML tag."""
    out: list[str] = []
    last = 0
    for i, ch in enumerate(segment):
        if ch != "<":
            continue
        if _ALLOWED_TAG_RE.match(segment, i):
            continue
        out.append(segment[last:i])
        out.append("&lt;")
        last = i + 1
    out.append(segment[last:])
    return "".join(out)


def _escape_outside_code(text: str) -> str:
    """Apply ``_escape_lt`` to everything except code fences / inline code."""
    out: list[str] = []
    last = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(_escape_lt(text[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_escape_lt(text[last:]))
    return "".join(out)


# A GFM table separator cell: dashes with optional alignment colons.
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _sub_wrap_row(line: str) -> str:
    """Wrap each cell of one table row in ``<sub>…</sub>``."""
    cells = line.strip().strip("|").split("|")
    if all(_TABLE_SEP_CELL_RE.match(c.strip()) for c in cells if c.strip()):
        return line  # separator row — keep alignment hints intact
    wrapped = [
        f" <sub>{c.strip()}</sub> "
        if c.strip() and not c.strip().startswith("<sub>")
        else c
        for c in cells
    ]
    return "|" + "|".join(wrapped) + "|"


def _sub_wrap_tables(text: str) -> str:
    """Shrink native-table font by wrapping cell contents in ``<sub>``.

    Bot API 10.1 offers no font-size control for tables and clients
    render them uncomfortably large; sub/superscript is the one inline
    style clients draw smaller. Only runs of >= 2 consecutive ``|``
    lines outside code fences are treated as tables — mirrors the
    detection in ``handlers.tg_format._table_rows``.
    """
    lines = text.split("\n")
    out = list(lines)
    in_fence = False
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence and lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    out[k] = _sub_wrap_row(lines[k])
            i = j
            continue
        i += 1
    return "\n".join(out)


def _render_details(m: re.Match[str]) -> str:
    """Render an expandable-quote sentinel block as a <details> block."""
    inner = m.group(1).strip()
    first = next((ln.strip() for ln in inner.splitlines() if ln.strip()), "…")
    if len(first) > _SUMMARY_MAX:
        first = first[: _SUMMARY_MAX - 1] + "…"
    return f"\n<details><summary>{first}</summary>\n\n{inner}\n\n</details>\n"


def to_rich_markdown(text: str) -> str:
    """Convert internal markdown to Rich Markdown for ``sendRichMessage``."""
    text = _escape_outside_code(text)
    text = _EXPQUOTE_RE.sub(_render_details, text)
    return _sub_wrap_tables(text)


def _input_rich_message(markdown: str) -> dict[str, Any]:
    return {"markdown": markdown}


async def send_rich_message(
    bot: ExtBot[Any],
    chat_id: int,
    markdown: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Send a rich message via the raw API; returns the sent Message."""
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _input_rich_message(markdown),
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    result = await bot._post("sendRichMessage", data)  # pyright: ignore[reportPrivateUsage]
    msg = Message.de_json(cast(dict[str, Any], result), bot)
    return msg


async def edit_rich_message(
    bot: ExtBot[Any],
    chat_id: int,
    message_id: int,
    markdown: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Replace a message's content with rich content via the raw API."""
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _input_rich_message(markdown),
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    await bot._post("editMessageText", data)  # pyright: ignore[reportPrivateUsage]
