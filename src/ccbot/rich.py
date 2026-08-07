"""Bot API 10.2 rich-message calls (sendRichMessage / rich editMessageText).

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
  - send_rich_message / edit_rich_message: thin raw-API wrappers with optional
    embedded photo upload/file-id reuse support.
  - extract_rich_photo_file_id: recover the best reusable photo file_id from
    a raw response or from the unknown-field payload in ``Message.api_kwargs``.

Key functions: to_rich_markdown, send_rich_message, edit_rich_message.
"""

import html
import re
from typing import Any, cast

from telegram import InlineKeyboardMarkup, InputFile, InputMediaPhoto, Message
from telegram.ext import ExtBot

from .transcript_format import (
    EXPANDABLE_HEADED_END,
    EXPANDABLE_HEADED_SEP,
    EXPANDABLE_HEADED_START,
    EXPANDABLE_QUOTE_END,
    EXPANDABLE_QUOTE_START,
)

# Rich messages cap (Bot API 10.2): 32768 UTF-8 chars of text.
RICH_MAX_CHARS = 32768

# One embedded terminal screenshot per rich message. The identifier connects
# the final Markdown media block to InputRichMessage.media; for a fresh upload
# it is also the multipart field name referenced via attach://.
_RICH_PHOTO_ID = "terminal_screenshot"
_RICH_PHOTO_UPLOAD_FIELD = _RICH_PHOTO_ID
_RICH_PHOTO_MARKDOWN = f"![](tg://photo?id={_RICH_PHOTO_ID})"
_RICH_PHOTO_FILENAME = "terminal_screenshot.png"
# Optional placement marker used by live cards. It is replaced only while
# building a rich photo payload and never reaches Telegram as visible text.
RICH_PHOTO_ANCHOR = "\x02RICH_PHOTO_ANCHOR\x02"

# Fenced code blocks (tolerating an unterminated fence at EOF) and inline
# code spans — `<` inside these is preserved verbatim by the rich parser.
_CODE_SPAN_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`")

# A one-line fenced block carries no layout information that an inline code
# span can't preserve. Telegram's current Rich Message clients expose Copy
# for the latter but not for fenced blocks, so normalize only this narrow
# case. Multi-line code, empty blocks, and commands containing a backtick
# remain fenced.
_SINGLE_LINE_FENCE_RE = re.compile(
    r"(?m)^[ \t]*```([^\n`]*)\n([^\n`]+)\n[ \t]*```[ \t]*$"
)
_COPYABLE_SHELL_LANGS = {"", "bash", "sh", "zsh", "shell", "console"}

# Telegram Android exposes its native tap/copy interaction for a multi-line
# Rich ``<code>`` span, but not for a Rich fenced block.  Convert shell fences
# only; non-shell fences retain their language highlighting and layout.
_FENCED_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*```([^\n`]*)\n(.*?)\n[ \t]*```[ \t]*(?=\n|$)"
)

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

_EXPHEADED_RE = re.compile(
    re.escape(EXPANDABLE_HEADED_START)
    + r"([\s\S]*?)"
    + re.escape(EXPANDABLE_HEADED_END)
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


def _inline_single_line_fences(text: str) -> str:
    """Turn a one-line shell fence into Telegram-copyable inline code."""

    def replace(match: re.Match[str]) -> str:
        language = match.group(1).strip().lower()
        if language not in _COPYABLE_SHELL_LANGS:
            return match.group(0)
        return f"`{match.group(2)}`"

    return _SINGLE_LINE_FENCE_RE.sub(replace, text)


def _multiline_shell_fences_to_code(text: str) -> str:
    """Render multi-line shell fences as copyable Rich ``code`` spans."""

    def replace(match: re.Match[str]) -> str:
        language = match.group(1).strip().lower()
        body = match.group(2)
        if language not in _COPYABLE_SHELL_LANGS or "\n" not in body:
            return match.group(0)
        # An unlabelled fence can also carry a literal table or prose. Keep a
        # pipe-table fenced: treating it as shell would change established
        # rendering even though it is not a command.
        body_lines = [line.strip() for line in body.splitlines() if line.strip()]
        if (
            not language
            and body_lines
            and all(line.startswith("|") for line in body_lines)
        ):
            return match.group(0)
        # Rich Markdown parses HTML inside <code>, so protect shell operators
        # and redirections while leaving quotes and newlines untouched.
        return f"<code>{html.escape(body, quote=False)}</code>"

    return _FENCED_BLOCK_RE.sub(replace, text)


# A GFM table separator cell: dashes with optional alignment colons.
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _is_sep_row(line: str) -> bool:
    """True if ``line`` is a GFM table separator row (``|---|:--:|``)."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c)


def _ensure_blank_before_tables(text: str) -> str:
    """Insert a blank line before a GFM table that abuts a text paragraph.

    CommonMark only recognises a pipe table when a blank line separates
    it from the paragraph above; otherwise the header row is absorbed
    into that paragraph and the rich parser emits plain text instead of
    a native ``table`` block. Session output routinely writes a caption
    (``**В работе**``) directly above the table, so we normalise it here.

    A table is a ``|``-led line whose NEXT line is a GFM separator row.
    We only inject when the previous emitted line is non-blank text (not
    itself a ``|`` row), and never inside fenced code blocks.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if (
            not in_fence
            and stripped.startswith("|")
            and idx + 1 < len(lines)
            and _is_sep_row(lines[idx + 1])
            and out
            and out[-1].strip()
            and not out[-1].lstrip().startswith("|")
        ):
            out.append("")
        out.append(line)
    return "\n".join(out)


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


def _render_details_headed(m: re.Match[str]) -> str:
    """Render the ``EXPANDABLE_HEADED`` sentinel as a ``<details>`` block
    with the explicit head as ``<summary>`` and the body WITHOUT a
    repeat of the head.
    """
    payload = m.group(1)
    head, _sep, body = payload.partition(EXPANDABLE_HEADED_SEP)
    head = head.strip() or "…"
    if len(head) > _SUMMARY_MAX:
        head = head[: _SUMMARY_MAX - 1] + "…"
    body = body.strip()
    if not body:
        return head
    return f"\n<details><summary>{head}</summary>\n\n{body}\n\n</details>\n"


def to_rich_markdown(text: str) -> str:
    """Convert internal markdown to Rich Markdown for ``sendRichMessage``."""
    text = _inline_single_line_fences(text)
    text = _ensure_blank_before_tables(text)
    text = _escape_outside_code(text)
    text = _EXPHEADED_RE.sub(_render_details_headed, text)
    text = _EXPQUOTE_RE.sub(_render_details, text)
    text = _sub_wrap_tables(text)
    return _multiline_shell_fences_to_code(text)


def _input_rich_message(markdown: str, photo_ref: str | None = None) -> dict[str, Any]:
    if photo_ref is None:
        return {"markdown": markdown.replace(RICH_PHOTO_ANCHOR, "")}
    media = InputMediaPhoto(media=photo_ref).to_dict()
    if RICH_PHOTO_ANCHOR in markdown:
        markdown = markdown.replace(RICH_PHOTO_ANCHOR, _RICH_PHOTO_MARKDOWN, 1)
        markdown = markdown.replace(RICH_PHOTO_ANCHOR, "")
    else:
        markdown = f"{markdown.rstrip()}\n\n{_RICH_PHOTO_MARKDOWN}"
    return {
        "markdown": markdown,
        "media": [{"id": _RICH_PHOTO_ID, "media": media}],
    }


def _photo_request_parts(photo: bytes | str) -> tuple[str, InputFile | None]:
    """Return the InputMediaPhoto reference and optional multipart upload."""
    if isinstance(photo, bytes):
        upload = InputFile(photo, filename=_RICH_PHOTO_FILENAME)
        return f"attach://{_RICH_PHOTO_UPLOAD_FIELD}", upload
    return photo, None


def _rich_message_payload(response: object) -> object | None:
    """Find the RichMessage object in a raw response or PTB Message."""
    if isinstance(response, Message):
        rich_message = getattr(response, "rich_message", None)
        if rich_message is not None:
            return rich_message
        return response.api_kwargs.get("rich_message")
    if isinstance(response, dict):
        return response.get("rich_message", response)
    api_kwargs = getattr(response, "api_kwargs", None)
    if isinstance(api_kwargs, dict):
        return api_kwargs.get("rich_message")
    return None


def extract_rich_photo_file_id(response: object) -> str | None:
    """Return the best reusable photo ``file_id`` from a rich response.

    Bot API 10.2 returns embedded photos inside ``rich_message.blocks`` rather
    than the legacy top-level ``Message.photo`` field. PTB versions that don't
    know RichMessage preserve that raw object in ``Message.api_kwargs``. This
    walker supports both forms, including photos nested in collage/slideshow
    blocks, and prefers the largest available PhotoSize by pixel area.
    """
    payload = _rich_message_payload(response)
    candidates: list[tuple[int, int, int, str]] = []
    order = 0

    def visit(value: object) -> None:
        nonlocal order
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict) and not isinstance(value, dict):
            value = to_dict()
        if isinstance(value, list | tuple):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        photo = value.get("photo")
        if value.get("type") == "photo" and isinstance(photo, list):
            for size in photo:
                if not isinstance(size, dict):
                    continue
                file_id = size.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    continue
                width = size.get("width")
                height = size.get("height")
                file_size = size.get("file_size")
                area = (
                    width * height
                    if isinstance(width, int) and isinstance(height, int)
                    else -1
                )
                size_bytes = file_size if isinstance(file_size, int) else -1
                candidates.append((area, size_bytes, order, file_id))
                order += 1
        for nested in value.values():
            if isinstance(nested, dict | list | tuple):
                visit(nested)

    if payload is not None:
        visit(payload)
    if not candidates:
        return None
    return max(candidates)[-1]


async def send_rich_message(
    bot: ExtBot[Any],
    chat_id: int,
    markdown: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    photo: bytes | str | None = None,
    disable_notification: bool | None = None,
) -> Message:
    """Send rich content, optionally ending with one embedded photo.

    ``photo`` accepts raw bytes for a multipart upload or a Telegram ``file_id``
    for reuse. Omitting it preserves the pre-10.2 request shape exactly.
    """
    photo_ref: str | None = None
    upload: InputFile | None = None
    if photo is not None:
        photo_ref, upload = _photo_request_parts(photo)
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _input_rich_message(markdown, photo_ref),
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    if disable_notification is not None:
        data["disable_notification"] = disable_notification
    if upload is not None:
        data[_RICH_PHOTO_UPLOAD_FIELD] = upload
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
    photo: bytes | str | None = None,
) -> Message | None:
    """Replace a message with rich content and an optional embedded photo."""
    photo_ref: str | None = None
    upload: InputFile | None = None
    if photo is not None:
        photo_ref, upload = _photo_request_parts(photo)
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": _input_rich_message(markdown, photo_ref),
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    if upload is not None:
        data[_RICH_PHOTO_UPLOAD_FIELD] = upload
    result = await bot._post(  # pyright: ignore[reportPrivateUsage]
        "editMessageText", data
    )
    if isinstance(result, Message):
        return result
    if isinstance(result, dict):
        return Message.de_json(result, bot)
    # Inline-message edits and lightweight test doubles may return True.
    return None
