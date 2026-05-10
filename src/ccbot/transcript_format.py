"""Tool-summary and tool-result formatting helpers — pulled out of
``transcript_parser.py`` to keep that file under the size budget.

These are pure functions over Claude transcript JSON shapes — no I/O,
no side effects. They turn raw tool_use / tool_result content blocks
into the short, human-readable strings the bot embeds in its session
cards and history pages.

Public API:
  EXPANDABLE_QUOTE_START / EXPANDABLE_QUOTE_END  — sentinel markers
      detected by ``markdown_v2`` to render an expandable Telegram
      blockquote. Plain ASCII so they survive any encoder.
  format_tool_use_summary(name, input) -> str
  extract_tool_result_text(content) -> str
  extract_tool_result_images(content) -> list[(media_type, bytes)] | None
  format_edit_diff(old, new) -> str  — compact unified diff
  format_expandable_quote(text) -> str
  format_tool_result_text(text, tool_name, tool_input) -> str
"""

from __future__ import annotations

import base64
import difflib
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Sentinels that survive arbitrary markdown re-rendering. Detected by
# markdown_v2.py to convert the wrapped block into a Telegram MarkdownV2
# expandable blockquote.
EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"

# Cap the length of the inline tool-summary tail (e.g. the file path
# after `Read(...)`). Longer summaries get truncated with an ellipsis.
_MAX_SUMMARY_LENGTH = 200


def format_edit_diff(old_string: str, new_string: str) -> str:
    """Compact unified diff (no --- / +++ header) between two strings."""
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
    out: list[str] = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line.rstrip("\n"))
    return "\n".join(out)


def format_tool_use_summary(name: str, input_data: dict | Any) -> str:
    """Format a tool_use block into a brief one-line summary.

    Returns ``**Name**(short summary)`` or ``**Name**`` when no useful
    summary can be extracted. Tool-specific selection prefers paths,
    commands, and patterns over generic dict values.
    """
    if not isinstance(input_data, dict):
        return f"**{name}**"

    summary = ""
    if name in ("Read", "Glob"):
        summary = input_data.get("file_path") or input_data.get("pattern", "")
    elif name == "Write":
        summary = input_data.get("file_path", "")
    elif name in ("Edit", "NotebookEdit"):
        summary = input_data.get("file_path") or input_data.get("notebook_path", "")
    elif name == "Bash":
        summary = input_data.get("command", "")
    elif name == "Grep":
        summary = input_data.get("pattern", "")
    elif name == "Task":
        summary = input_data.get("description", "")
    elif name == "WebFetch":
        summary = input_data.get("url", "")
    elif name == "WebSearch":
        summary = input_data.get("query", "")
    elif name == "TodoWrite":
        todos = input_data.get("todos", [])
        if isinstance(todos, list):
            summary = f"{len(todos)} item(s)"
    elif name == "TodoRead":
        summary = ""
    elif name == "AskUserQuestion":
        questions = input_data.get("questions", [])
        if isinstance(questions, list) and questions:
            q = questions[0]
            if isinstance(q, dict):
                summary = q.get("question", "")
    elif name == "ExitPlanMode":
        summary = ""
    elif name == "Skill":
        summary = input_data.get("skill", "")
    else:
        for v in input_data.values():
            if isinstance(v, str) and v:
                summary = v
                break

    if summary:
        if len(summary) > _MAX_SUMMARY_LENGTH:
            summary = summary[:_MAX_SUMMARY_LENGTH] + "…"
        return f"**{name}**({summary})"
    return f"**{name}**"


def extract_tool_result_text(content: list | Any) -> str:
    """Concatenate all text fragments from a tool_result content block."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def extract_tool_result_images(
    content: list | Any,
) -> list[tuple[str, bytes]] | None:
    """Extract base64 images from a tool_result content block.

    Returns a list of ``(media_type, raw_bytes)`` or None if no images.
    """
    if not isinstance(content, list):
        return None
    images: list[tuple[str, bytes]] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        source = item.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        media_type = source.get("media_type", "image/png")
        data_str = source.get("data", "")
        if not data_str:
            continue
        try:
            raw_bytes = base64.b64decode(data_str)
            images.append((media_type, raw_bytes))
        except Exception:
            logger.debug("Failed to decode base64 image in tool_result")
    return images if images else None


def format_expandable_quote(text: str) -> str:
    """Wrap text with sentinel markers for downstream MarkdownV2 conversion."""
    return f"{EXPANDABLE_QUOTE_START}{text}{EXPANDABLE_QUOTE_END}"


def format_tool_result_text(
    text: str,
    tool_name: str | None = None,
    tool_input_data: dict | None = None,
) -> str:
    """Format tool result text with per-tool statistics + expandable quote.

    No truncation here — splitting respects the 4096-char Telegram cap
    only at the send layer (`split_message`).
    """
    if not text:
        return ""

    line_count = text.count("\n") + 1 if text else 0

    if tool_name == "Read":
        return f"  ⎿  Read {line_count} lines"

    if tool_name == "Write":
        written = tool_input_data.get("content", "") if tool_input_data else ""
        if not written:
            written_lines = line_count
        else:
            written_lines = written.count("\n") + (
                0 if written.endswith("\n") else 1
            )
        return f"  ⎿  Wrote {written_lines} lines"

    if tool_name == "Bash":
        if line_count > 0:
            stats = f"  ⎿  Output {line_count} lines"
            return stats + "\n" + format_expandable_quote(text)
        return format_expandable_quote(text)

    if tool_name == "Grep":
        matches = len([line for line in text.split("\n") if line.strip()])
        stats = f"  ⎿  Found {matches} matches"
        return stats + "\n" + format_expandable_quote(text)

    if tool_name == "Glob":
        files = len([line for line in text.split("\n") if line.strip()])
        stats = f"  ⎿  Found {files} files"
        return stats + "\n" + format_expandable_quote(text)

    if tool_name == "Task":
        if line_count > 0:
            stats = f"  ⎿  Agent output {line_count} lines"
            return stats + "\n" + format_expandable_quote(text)
        return format_expandable_quote(text)

    if tool_name == "WebFetch":
        char_count = len(text)
        stats = f"  ⎿  Fetched {char_count} characters"
        return stats + "\n" + format_expandable_quote(text)

    if tool_name == "WebSearch":
        results = text.count("\n\n") + 1 if text else 0
        stats = f"  ⎿  {results} search results"
        return stats + "\n" + format_expandable_quote(text)

    return format_expandable_quote(text)
