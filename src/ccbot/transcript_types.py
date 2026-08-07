"""Public value objects produced by transcript parsing."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedMessage:
    """Parsed message from a transcript."""

    message_type: str  # "user", "assistant", "tool_use", "tool_result", etc.
    text: str  # Extracted text content
    tool_name: str | None = None  # For tool_use messages


@dataclass
class ParsedEntry:
    """A single parsed message entry ready for display."""

    role: str  # "user" | "assistant"
    text: str  # Already formatted text
    content_type: (
        str  # "text" | "thinking" | "tool_use" | "tool_result" | "local_command"
    )
    tool_use_id: str | None = None
    timestamp: str | None = None  # ISO timestamp from JSONL
    tool_name: str | None = (
        None  # For tool_use entries, the tool name (e.g. "AskUserQuestion")
    )
    image_data: list[tuple[str, bytes]] | None = (
        None  # For tool_result entries with images: (media_type, raw_bytes)
    )
    stop_reason: str | None = (
        None  # Assistant message stop_reason: "end_turn" | "tool_use" | etc.
    )
    # ``is_error=True`` when the tool_result block carried ``is_error: true``
    # in the JSONL — propagates through NewMessage and lands on the matching
    # tool_use Event so ``render_event`` can flip the leading glyph to ✗.
    is_error: bool = False
    # Claude Code marks its own synthetic error turns at the entry level:
    # ``isApiErrorMessage: true`` plus an ``error`` code (e.g.
    # "authentication_failed"). Carried through so consumers can tell a real
    # API failure from an assistant that merely *talks about* one — matching
    # error text against arbitrary assistant output produces false positives.
    api_error: str = ""


@dataclass
class PendingToolInfo:
    """Information about a pending tool_use waiting for its tool_result."""

    summary: str  # Formatted tool summary (e.g. "**Read**(file.py)")
    tool_name: str  # Tool name (e.g. "Read", "Edit")
    input_data: Any = None  # Tool input parameters (for Edit to generate diff)
