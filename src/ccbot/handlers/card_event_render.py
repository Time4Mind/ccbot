"""Render individual live-card events and their expandable tool bodies."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from .card_budget import _format_elapsed, _format_hhmm, _trimmed_body
from .card_types import Event

__all__ = [
    "_EXT_TO_LANG",
    "_lang_for_path",
    "_format_tool_args",
    "_format_tool_content",
    "_build_tool_spoiler_body",
    "_spoiler_body",
    "_headed_block",
    "render_event",
]

# File-extension → language hint for syntax-highlighted fenced code
# blocks inside a tool's spoiler body. Telegram's rich-message rendering
# accepts these as the info string after ```` ``` ````.
_EXT_TO_LANG: dict[str, str] = {
    "py": "python",
    "ts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "jsx": "jsx",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "md": "markdown",
    "sql": "sql",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "sh": "bash",
    "bash": "bash",
    "zsh": "bash",
    "c": "c",
    "cpp": "cpp",
    "h": "c",
    "hpp": "cpp",
    "rb": "ruby",
    "php": "php",
    "swift": "swift",
    "lua": "lua",
    "xml": "xml",
}


def _lang_for_path(path: str) -> str:
    """Pick a syntax-highlight language hint from a file path's extension.

    Returns an empty string when the extension is unknown / absent —
    callers should fall back to a no-language fenced block (still
    monospace, just no highlighting).
    """
    if not path:
        return ""
    basename = path.rsplit("/", 1)[-1]
    if basename == "Dockerfile" or basename.lower().endswith(".dockerfile"):
        return "dockerfile"
    if "." not in basename:
        return ""
    ext = basename.rsplit(".", 1)[-1].lower()
    return _EXT_TO_LANG.get(ext, "")


def _format_tool_args(tool_name: str, args: str) -> str:
    """Wrap a tool's args (command / path / pattern / URL) for visual
    contrast inside the spoiler body — Bash gets a ``bash`` fenced
    block (syntax highlighting), everything else gets an inline
    ``code`` span.
    """
    if not args:
        return ""
    if tool_name == "Bash":
        return f"```bash\n{args}\n```"
    return f"`{args}`"


def _format_tool_content(tool_name: str, args: str, content: str) -> str:
    """Wrap a tool's content block (file body / diff) when it's actual
    code — Read/Write content gets a fenced block in the file's
    language, Edit content gets a ``diff`` block. Bash stdout, Grep
    matches, WebFetch / WebSearch text are NOT code and stay plain.
    """
    if not content:
        return ""
    if tool_name in ("Read", "Write"):
        lang = _lang_for_path(args)
        return f"```{lang}\n{content}\n```" if lang else f"```\n{content}\n```"
    if tool_name == "Edit":
        return f"```diff\n{content}\n```"
    return content


def _bounded_tool_block(text: str, max_lines: int, max_chars: int = 100) -> str:
    """Keep one command/result block within the phone-readable budget."""
    if not text:
        return ""
    lines = text.splitlines() or [text]
    bounded = [
        line if len(line) <= max_chars else line[: max_chars - 1] + "…"
        for line in lines
    ]
    if len(bounded) > max_lines:
        hidden = len(bounded) - max_lines + 1
        bounded = bounded[: max_lines - 1] + [f"… (+{hidden} more lines)"]
    return "\n".join(bounded)


def _scalar_text(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _structured_value_lines(value: Any, indent: int = 0) -> list[str]:
    """Render parsed JSON as a compact, readable key/value hierarchy."""
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, child in value.items():
            label = str(key)
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}{label}:")
                lines.extend(_structured_value_lines(child, indent + 2))
            else:
                scalar = _scalar_text(child)
                if "\n" in scalar:
                    lines.append(f"{prefix}{label}:")
                    lines.extend(
                        f"{' ' * (indent + 2)}{row}" for row in scalar.splitlines()
                    )
                else:
                    lines.append(f"{prefix}{label}: {scalar}")
        return lines
    if isinstance(value, list):
        lines = []
        for child in value:
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_structured_value_lines(child, indent + 2))
            else:
                scalar = _scalar_text(child)
                child_lines = scalar.splitlines() or [""]
                lines.append(f"{prefix}- {child_lines[0]}")
                lines.extend(f"{' ' * (indent + 2)}{row}" for row in child_lines[1:])
        return lines
    return [f"{prefix}{_scalar_text(value)}"]


_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][\w ./-]{0,79})=(.*)$")


def _structure_key_values(text: str) -> str | None:
    rows = text.splitlines()
    if not rows:
        return None
    parsed: list[tuple[str, str]] = []
    for row in rows:
        match = _KEY_VALUE_RE.fullmatch(row.strip())
        if match is None:
            return None
        parsed.append((match.group(1).strip(), match.group(2).strip()))
    return "\n".join(f"{key}: {value}" for key, value in parsed)


def _structure_table(text: str) -> str | None:
    """Expand an unambiguous delimited table into labelled records."""
    lines = [row for row in text.splitlines() if row.strip()]
    if len(lines) < 2:
        return None
    delimiter = next(
        (
            candidate
            for candidate in ("\t", "|", ",")
            if all(candidate in row for row in lines)
        ),
        None,
    )
    if delimiter is None:
        return None
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        return None
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows):
        return None
    headers = [cell.strip() for cell in rows[0]]
    if any(not header for header in headers) or len(set(headers)) != width:
        return None
    output: list[str] = []
    data_rows = rows[1:]
    for number, row in enumerate(data_rows, 1):
        if len(data_rows) > 1:
            output.append(f"record {number}:")
            pad = "  "
        else:
            pad = ""
        output.extend(
            f"{pad}{header}: {value.strip()}"
            for header, value in zip(headers, row, strict=True)
        )
    return "\n".join(output)


def _structure_tool_result(text: str) -> str:
    """Conservatively structure known result shapes before truncation."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    try:
        parsed = json.loads(normalized)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    else:
        if isinstance(parsed, str):
            return parsed
        return "\n".join(_structured_value_lines(parsed))

    json_rows: list[Any] = []
    raw_rows = normalized.splitlines()
    if len(raw_rows) > 1:
        try:
            json_rows = [json.loads(row) for row in raw_rows]
        except (json.JSONDecodeError, TypeError):
            json_rows = []
    if json_rows and all(isinstance(row, (dict, list)) for row in json_rows):
        lines: list[str] = []
        for number, row in enumerate(json_rows, 1):
            lines.append(f"record {number}:")
            lines.extend(_structured_value_lines(row, 2))
        return "\n".join(lines)

    # Codex exec results carry stable metadata followed by an ``output:``
    # block. Structure the payload independently while retaining that useful
    # metadata and its explicit boundary.
    prefix, marker, payload = normalized.partition("\noutput:\n")
    if marker:
        structured_payload = _structure_tool_result(payload)
        if structured_payload:
            indented = "\n".join(f"  {row}" for row in structured_payload.splitlines())
            return f"{prefix}\noutput:\n{indented}"
        return f"{prefix}\noutput: no output"

    key_values = _structure_key_values(normalized)
    if key_values is not None:
        return key_values
    table = _structure_table(normalized)
    if table is not None:
        return table
    return normalized


def _build_tool_spoiler_body(
    tool_name: str,
    args: str,
    content: str,
    *,
    command_max_lines: int = 10,
    result_max_lines: int = 10,
) -> str:
    """Assemble the spoiler body for a tool event — args first
    (highlighted), then content (highlighted when it's code)."""
    parts: list[str] = []
    bounded_args = _bounded_tool_block(args, command_max_lines)
    structured_content = (
        content
        if tool_name in ("Read", "Write", "Edit", "NotebookEdit")
        else _structure_tool_result(content)
    )
    bounded_content = _bounded_tool_block(structured_content, result_max_lines)
    if bounded_args:
        parts.append(_format_tool_args(tool_name, bounded_args))
    if bounded_content:
        parts.append(_format_tool_content(tool_name, args, bounded_content))
    return "\n- - -\n".join(parts)


def _spoiler_body(body: str) -> str:
    """Legacy plain-expandable wrapper kept for callers (tests, the
    notifications re-export) that don't pair a head with the body.

    ``render_event`` itself moved to :func:`_headed_block` so the tool
    event line becomes the spoiler label instead of sitting on its own
    above a plain spoiler. New code shouldn't call this directly.
    """
    from ..transcript_format import format_expandable_quote

    trimmed = _trimmed_body(body)
    if not trimmed:
        return ""
    return format_expandable_quote(trimmed)


def _headed_block(head: str, body: str, *, trim_body: bool = True) -> str:
    """Return ``head`` if there's no body, else wrap ``(head, body)``
    in the ``EXPANDABLE_HEADED`` sentinel so the rich renderer makes
    ``head`` the spoiler label and ``body`` the collapsible content
    (without repeating the head)."""
    from ..transcript_format import format_expandable_with_head

    trimmed = _trimmed_body(body) if trim_body else body
    if not trimmed:
        return head
    return format_expandable_with_head(head, trimmed)


def render_event(
    event: Event,
    *,
    in_flight: bool,
    now: float,
    spoiler_line_limits: tuple[int, int] = (10, 10),
) -> str:
    """Render one Event as a plain-text block for the card."""
    # Build the trailing time-or-elapsed marker
    if in_flight:
        marker = f" · ⏳ {_format_elapsed(now - event.started_at)}"
    elif event.type in ("tool_use", "thinking", "text"):
        marker = f" · {_format_hhmm(event.started_at)}"
    else:
        marker = ""

    if event.type == "user_msg":
        return f"{event.user_icon} {event.text}"

    if event.type == "thinking":
        return _headed_block(f"∴ thinking{marker}", event.body)

    if event.type == "tool_use":
        if event.is_error:
            glyph = "✗"
        elif in_flight:
            glyph = "▷"
        else:
            glyph = "✓"
        if event.tool_args or event.tool_content:
            body = _build_tool_spoiler_body(
                event.tool_name or event.text,
                event.tool_args,
                event.tool_content,
                command_max_lines=spoiler_line_limits[0],
                result_max_lines=spoiler_line_limits[1],
            )
            return _headed_block(f"{glyph} {event.text}{marker}", body, trim_body=False)
        return _headed_block(f"{glyph} {event.text}{marker}", event.body)

    if event.type == "tool_result":
        # Fallback when the matching tool_use Event isn't found (parser
        # race / restart). Render as a standalone row.
        if event.tool_args or event.tool_content:
            body = _build_tool_spoiler_body(
                event.tool_name or event.text,
                event.tool_args,
                event.tool_content,
                command_max_lines=spoiler_line_limits[0],
                result_max_lines=spoiler_line_limits[1],
            )
            return _headed_block(f"✓ {event.text}{marker}", body, trim_body=False)
        return _headed_block(f"✓ {event.text}{marker}", event.body)

    if event.type in ("text", "final_text", "error"):
        # Mid-stream / final / error — inline, no glyph.
        return event.text

    return event.text
