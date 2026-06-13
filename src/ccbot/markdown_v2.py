"""Markdown → Telegram MarkdownV2 conversion layer.

Wraps `telegramify_markdown` and adds special handling for expandable
blockquotes (delimited by sentinel tokens from TranscriptParser).
Expandable quotes are escaped and formatted as Telegram >…|| syntax
separately, so the library doesn't mangle them.

Key function: convert_markdown(text) → MarkdownV2 string.
"""

import re

import mistletoe
from mistletoe.block_token import BlockCode, remove_token
from telegramify_markdown import _update_block, escape_latex  # pyright: ignore[reportPrivateUsage]
from telegramify_markdown.render import TelegramMarkdownRenderer

from .transcript_parser import TranscriptParser

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format.

    Telegram has no table rendering. This converts each row into a card
    with **Header**: value pairs, separated by horizontal lines — similar
    to how Claude Code renders tables in narrow terminals.

    Skips tables inside code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Check if this looks like a table header row
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        ):
            headers = _split_table_row(stripped)

            # Next line must be separator (---|---|---)
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and _TABLE_SEP_RE.match(sep_line):
                    i += 2  # Skip header + separator
                    rows: list[list[str]] = []
                    while i < len(lines):
                        data_line = lines[i].strip()
                        if data_line.startswith("|") and data_line.endswith("|"):
                            rows.append(_split_table_row(data_line))
                            i += 1
                        else:
                            break

                    # Build card-style output
                    separator = "────────────"
                    cards: list[str] = []
                    for row in rows:
                        card_lines: list[str] = []
                        for j, header in enumerate(headers):
                            value = row[j] if j < len(row) else ""
                            if value:
                                card_lines.append(f"**{header}**: {value}")
                            else:
                                card_lines.append(f"**{header}**: —")
                        cards.append("\n".join(card_lines))

                    result.append(f"\n{separator}\n".join(cards))
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


_EXPQUOTE_RE = re.compile(
    re.escape(TranscriptParser.EXPANDABLE_QUOTE_START)
    + r"[\s\S]*?"
    + re.escape(TranscriptParser.EXPANDABLE_QUOTE_END)
)

# Characters that must be escaped in Telegram MarkdownV2 plain text
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


# Max rendered chars for a single expandable quote block.
# Leaves room for surrounding text within Telegram's 4096 char message limit.
_EXPQUOTE_MAX_RENDERED = 3800


def _quote_lines(inner: str) -> str:
    """Render a piece of inner text as a MarkdownV2 expandable blockquote.

    Truncates output to ``_EXPQUOTE_MAX_RENDERED`` chars so we stay
    inside Telegram's 4096-char message budget no matter what the
    caller shoves in.
    """
    escaped = _escape_mdv2(inner)
    lines = escaped.split("\n")
    built: list[str] = []
    total_len = 0
    suffix = "\n>… \\(truncated\\)||"
    budget = _EXPQUOTE_MAX_RENDERED - len(suffix)
    truncated = False
    for line in lines:
        # +1 for ">" prefix, +1 for "\n" separator
        line_cost = 1 + len(line) + 1
        if total_len + line_cost > budget:
            remaining = budget - total_len - 2  # -2 for ">" and "\n"
            if remaining > 20:
                built.append(f">{line[:remaining]}")
            truncated = True
            break
        built.append(f">{line}")
        total_len += line_cost
    if truncated:
        return "\n".join(built) + suffix
    return "\n".join(built) + "||"


def _markdownify(text: str) -> str:
    """Custom markdownify with our rendering rules.

    Wraps TelegramMarkdownRenderer directly (instead of calling
    telegramify_markdown.markdownify) so we can tweak token rules
    inside the context manager — reset_tokens() in __exit__ would
    otherwise undo any module-level changes.

    Custom rules:
      - Disable indented code blocks (only fenced ``` blocks are code).
    """
    with TelegramMarkdownRenderer(normalize_whitespace=False) as renderer:
        remove_token(BlockCode)
        content = escape_latex(text)
        document = mistletoe.Document(content)
        _update_block(document)
        return renderer.render(document)


_TAG_TO_MD: dict[str, tuple[str, str]] = {
    "b": ("**", "**"),
    "strong": ("**", "**"),
    "i": ("*", "*"),
    "em": ("*", "*"),
    "u": ("__", "__"),
    "s": ("~", "~"),
    "code": ("`", "`"),
    "pre": ("```", "```"),
}
_HTML_TAG_RE = re.compile(
    r"<(b|strong|i|em|u|s|code|pre)>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_FENCE_SPLIT_RE = re.compile(r"(```.*?```)", re.DOTALL)


def _html_inline_to_markdown(text: str) -> str:
    """Rewrite the Telegram-HTML inline tags Claude sometimes emits
    (``<b>``, ``<i>``, ``<code>``, ``<pre>``, friends) into Markdown
    equivalents so the rest of the pipeline (telegramify-markdown →
    MarkdownV2) can render them. Without this, claude sessions whose
    instructions push HTML parse_mode land their literal ``<b>…</b>``
    text in chat — the pets-session "formatting broken" symptom.

    Contents inside triple-backtick fenced code blocks are left alone
    so we don't corrupt a code example that's discussing the tags
    themselves. Nested tags resolve via an iterative pass (capped to
    avoid pathological loops).
    """

    def _repl(m: re.Match[str]) -> str:
        tag = m.group(1).lower()
        inner = m.group(2)
        left, right = _TAG_TO_MD[tag]
        return f"{left}{inner}{right}"

    parts = _FENCE_SPLIT_RE.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Inside a ``` fence — leave as is.
            out.append(part)
            continue
        # Outside — convert HTML inline tags, looping for nested cases.
        for _ in range(5):
            new = _HTML_TAG_RE.sub(_repl, part)
            if new == part:
                break
            part = new
        out.append(part)
    return "".join(out)


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Expandable blockquote sections (marked by sentinel tokens from
    TranscriptParser) are extracted, escaped, and formatted separately
    so that telegramify_markdown doesn't mangle the >...|| syntax.
    """
    # Pre-convert Telegram-HTML inline tags (<b>/<i>/<code>/<pre>) that
    # claude sometimes emits — see ``_html_inline_to_markdown``. Done
    # before everything else so downstream tables / quote-block /
    # markdown passes operate on uniform Markdown input.
    text = _html_inline_to_markdown(text)

    # Convert markdown tables to card-style format before telegramify
    text = convert_markdown_tables(text)

    # Extract expandable-quote blocks before telegramify processes the
    # rest — telegramify mangles the >…|| syntax otherwise.
    segments: list[tuple[bool, str]] = []  # (is_quote, content)
    last_end = 0
    for m in _EXPQUOTE_RE.finditer(text):
        if m.start() > last_end:
            segments.append((False, text[last_end : m.start()]))
        segments.append((True, m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append((False, text[last_end:]))

    if not segments:
        return _markdownify(text)

    parts: list[str] = []
    for is_quote, segment in segments:
        if is_quote:
            inner = segment[
                len(TranscriptParser.EXPANDABLE_QUOTE_START) : -len(
                    TranscriptParser.EXPANDABLE_QUOTE_END
                )
            ]
            parts.append(_quote_lines(inner))
        else:
            parts.append(_markdownify(segment))
    return "".join(parts)
