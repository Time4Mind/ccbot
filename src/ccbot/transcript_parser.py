"""JSONL transcript parser for Claude Code session files.

Parses Claude Code session JSONL files and extracts structured messages.
Handles: text, thinking, tool_use, tool_result, local_command, and user messages.
Tool pairing: tool_use blocks in assistant messages are matched with
tool_result blocks in subsequent user messages via tool_use_id.

Shared by both session.py (history) and session_monitor.py (real-time).
Tool-summary and tool-result formatting helpers live in
``transcript_format`` — re-exported here for backward compatibility.

Key classes: TranscriptParser (static methods), ParsedEntry, ParsedMessage, PendingToolInfo.
"""

import logging
import re
from typing import Any

from . import transcript_format
from .transcript_codex import normalize_codex_entry
from .transcript_format import (
    EXPANDABLE_HEADED_END,
    EXPANDABLE_HEADED_SEP,
    EXPANDABLE_HEADED_START,
    EXPANDABLE_QUOTE_END,
    EXPANDABLE_QUOTE_START,
)
from .transcript_message import extract_text_only as _extract_text_only
from .transcript_message import get_message_type as _get_message_type
from .transcript_message import get_timestamp as _get_timestamp
from .transcript_message import is_user_message as _is_user_message
from .transcript_message import parse_line as _parse_line
from .transcript_message import parse_message as _parse_message
from .transcript_types import ParsedEntry, ParsedMessage, PendingToolInfo

logger = logging.getLogger(__name__)

# Preserve the historical pickle/introspection path of public value types even
# though their definitions now live in a dependency-light sibling module.
ParsedMessage.__module__ = __name__
ParsedEntry.__module__ = __name__
PendingToolInfo.__module__ = __name__


class TranscriptParser:
    """Parser for Claude Code JSONL session files.

    Expected JSONL entry structure:
    - type: "user" | "assistant" | "summary" | "file-history-snapshot" | ...
    - message.content: list[Any] of blocks (text, tool_use, tool_result, thinking)
    - sessionId, cwd, timestamp, uuid: metadata fields

    Tool pairing model: tool_use blocks appear in assistant messages,
    matching tool_result blocks appear in the next user message (keyed by tool_use_id).
    """

    # Magic string constants
    _NO_CONTENT_PLACEHOLDER = "(no content)"
    _INTERRUPTED_TEXT = "[Request interrupted by user for tool use]"

    # Re-export of expandable-quote sentinels for callers that still
    # reach in via TranscriptParser (markdown_v2, message_sender).
    EXPANDABLE_QUOTE_START = EXPANDABLE_QUOTE_START
    EXPANDABLE_QUOTE_END = EXPANDABLE_QUOTE_END
    EXPANDABLE_HEADED_START = EXPANDABLE_HEADED_START
    EXPANDABLE_HEADED_END = EXPANDABLE_HEADED_END
    EXPANDABLE_HEADED_SEP = EXPANDABLE_HEADED_SEP

    @staticmethod
    def parse_line(line: str) -> dict[str, Any] | None:
        """Parse one JSONL line, returning ``None`` when it is invalid."""
        return _parse_line(line)

    @staticmethod
    def get_message_type(data: dict[str, Any]) -> str | None:
        """Get the top-level transcript message type."""
        return _get_message_type(data)

    @staticmethod
    def is_user_message(data: dict[str, Any]) -> bool:
        """Return whether this is a user message."""
        return _is_user_message(data)

    @staticmethod
    def extract_text_only(content_list: list[Any]) -> str:
        """Extract text blocks, excluding tool calls and thinking blocks."""
        return _extract_text_only(content_list)

    _RE_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

    _RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>")
    _RE_LOCAL_STDOUT = re.compile(
        r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
    )
    _RE_SYSTEM_TAGS = re.compile(
        r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)"
    )

    @staticmethod
    def _normalize_codex_entry(data: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize one supported Codex rollout row into Claude shape."""
        return normalize_codex_entry(data)

    @classmethod
    def parse_message(cls, data: dict[str, Any]) -> ParsedMessage | None:
        return _parse_message(cls, data)

    @staticmethod
    def get_timestamp(data: dict[str, Any]) -> str | None:
        """Extract timestamp from message data."""
        return _get_timestamp(data)

    @classmethod
    def parse_entries(
        cls,
        entries: list[dict[str, Any]],
        pending_tools: dict[str, PendingToolInfo] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, PendingToolInfo]]:
        """Parse a list[Any] of JSONL entries into a flat list[Any] of display-ready messages.

        This is the shared core logic used by both get_recent_messages (history)
        and check_for_updates (monitor).

        Args:
            entries: List of parsed JSONL dicts (already filtered through parse_line)
            pending_tools: Optional carry-over pending tool_use state from a
                previous call (tool_use_id -> formatted summary). Used by the
                monitor to handle tool_use and tool_result arriving in separate
                poll cycles.

        Returns:
            Tuple of (parsed entries, remaining pending_tools state)
        """
        result: list[ParsedEntry] = []
        last_cmd_name: str | None = None
        # Pending tool_use blocks keyed by id
        _carry_over = pending_tools is not None
        if pending_tools is None:
            pending_tools = {}
        else:
            pending_tools = dict[str, Any](
                pending_tools
            )  # don't mutate caller's dict[str, Any]

        for raw_data in entries:
            data = raw_data
            if data.get("type") not in ("user", "assistant"):
                normalized = cls._normalize_codex_entry(data)
                if normalized is None:
                    continue
                data = normalized
            msg_type = cls.get_message_type(data)
            if msg_type not in ("user", "assistant"):
                continue

            # Extract timestamp for this entry
            entry_timestamp = cls.get_timestamp(data)

            message = data.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content)}] if content else []

            parsed = cls.parse_message(data)

            # Handle local command messages first
            if parsed:
                if parsed.message_type == "local_command_invoke":
                    last_cmd_name = parsed.tool_name
                    continue
                if parsed.message_type == "local_command":
                    cmd = parsed.tool_name or last_cmd_name or ""
                    text = parsed.text
                    if cmd:
                        if "\n" in text:
                            formatted = f"❯ `{cmd}`\n```\n{text}\n```"
                        else:
                            formatted = f"❯ `{cmd}`\n`{text}`"
                    else:
                        if "\n" in text:
                            formatted = f"```\n{text}\n```"
                        else:
                            formatted = f"`{text}`"
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text=formatted,
                            content_type="local_command",
                            timestamp=entry_timestamp,
                        )
                    )
                    last_cmd_name = None
                    continue
            last_cmd_name = None

            if msg_type == "assistant":
                # Process content blocks
                has_text = False
                stop_reason = message.get("stop_reason")
                pre_count = len(result)
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t and t != cls._NO_CONTENT_PLACEHOLDER:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=t,
                                    content_type="text",
                                    timestamp=entry_timestamp,
                                )
                            )
                            has_text = True

                    elif btype == "tool_use":
                        tool_id = block.get("id", "")
                        name = block.get("name", "unknown")
                        inp = block.get("input", {})
                        summary = transcript_format.format_tool_use_summary(name, inp)

                        # ExitPlanMode: emit plan content as text before tool_use entry
                        if name == "ExitPlanMode" and isinstance(inp, dict):
                            plan = inp.get("plan", "")
                            if plan:
                                result.append(
                                    ParsedEntry(
                                        role="assistant",
                                        text=plan,
                                        content_type="text",
                                        timestamp=entry_timestamp,
                                    )
                                )
                        if tool_id:
                            # Store tool info for later tool_result formatting
                            # Edit tool needs input_data to generate diff in tool_result stage
                            input_data = (
                                inp
                                if name in ("Edit", "NotebookEdit", "Write")
                                else None
                            )
                            pending_tools[tool_id] = PendingToolInfo(
                                summary=summary,
                                tool_name=name,
                                input_data=input_data,
                            )
                            # Also emit tool_use entry with tool_name for immediate handling
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                )
                            )
                        else:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id or None,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                )
                            )

                    elif btype == "thinking":
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            quoted = transcript_format.format_expandable_quote(
                                thinking_text
                            )
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=quoted,
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                )
                            )
                        elif not has_text:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text="(thinking)",
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                )
                            )

                # Stamp stop_reason on every entry produced from this assistant
                # message — bot.py uses it to distinguish intermediate text
                # (stop_reason="tool_use") from a real end-of-turn ("end_turn").
                # ``api_error`` rides along the same way: Claude Code writes its
                # own failures as synthetic assistant turns flagged at the entry
                # level (``isApiErrorMessage`` + ``error`` code).
                api_error = ""
                if data.get("isApiErrorMessage"):
                    api_error = str(data.get("error") or "unknown")
                for entry in result[pre_count:]:
                    entry.stop_reason = stop_reason
                    entry.api_error = api_error

            elif msg_type == "user":
                # Check for tool_result blocks and merge with pending tools
                user_text_parts: list[str] = []

                for block in content:
                    if not isinstance(block, dict):
                        if isinstance(block, str) and block.strip():
                            user_text_parts.append(block.strip())
                        continue
                    btype = block.get("type", "")

                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        result_text = transcript_format.extract_tool_result_text(
                            result_content
                        )
                        result_images = transcript_format.extract_tool_result_images(
                            result_content
                        )
                        is_error = block.get("is_error", False)
                        is_interrupted = result_text == cls._INTERRUPTED_TEXT
                        tool_info = pending_tools.pop(tool_use_id, None)
                        _tuid = tool_use_id or None

                        # Extract tool info from PendingToolInfo object
                        if tool_info is None:
                            tool_summary = None
                            tool_name = None
                            tool_input_data = None
                        else:
                            tool_summary = tool_info.summary
                            tool_name = tool_info.tool_name
                            tool_input_data = tool_info.input_data

                        if is_interrupted:
                            # Show interruption inline with tool summary
                            entry_text = tool_summary or ""
                            if entry_text:
                                entry_text += "\n⏹ Interrupted"
                            else:
                                entry_text = "⏹ Interrupted"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                )
                            )
                        elif is_error:
                            # Show error in stats line
                            if tool_summary:
                                entry_text = tool_summary
                            else:
                                entry_text = "**Error**"
                            # Add error message in stats format
                            if result_text:
                                # Take first line of error as summary
                                error_summary = result_text.split("\n")[0]
                                if len(error_summary) > 100:
                                    error_summary = error_summary[:100] + "…"
                                entry_text += f"\n  ⎿  Error: {error_summary}"
                                # If multi-line error, add expandable quote
                                if "\n" in result_text:
                                    entry_text += (
                                        "\n"
                                        + transcript_format.format_expandable_quote(
                                            result_text
                                        )
                                    )
                            else:
                                entry_text += "\n  ⎿  Error"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                    is_error=True,
                                )
                            )
                        elif tool_summary:
                            entry_text = tool_summary
                            # For Edit tool, generate diff stats and expandable quote
                            if tool_name == "Edit" and tool_input_data and result_text:
                                old_s = tool_input_data.get("old_string", "")
                                new_s = tool_input_data.get("new_string", "")
                                if old_s and new_s:
                                    diff_text = transcript_format.format_edit_diff(
                                        old_s, new_s
                                    )
                                    if diff_text:
                                        added = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("+")
                                            and not line.startswith("+++")
                                        )
                                        removed = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("-")
                                            and not line.startswith("---")
                                        )
                                        stats = f"  ⎿  Added {added} lines, removed {removed} lines"
                                        entry_text += (
                                            "\n"
                                            + stats
                                            + "\n"
                                            + transcript_format.format_expandable_quote(
                                                diff_text
                                            )
                                        )
                            # For other tools, append formatted result text
                            elif (
                                result_text
                                and cls.EXPANDABLE_QUOTE_START not in tool_summary
                            ):
                                entry_text += (
                                    "\n"
                                    + transcript_format.format_tool_result_text(
                                        result_text, tool_name, tool_input_data
                                    )
                                )
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                )
                            )
                        elif result_text or result_images:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=transcript_format.format_tool_result_text(
                                        result_text, tool_name, tool_input_data
                                    )
                                    if result_text
                                    else (tool_summary or ""),
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                )
                            )

                    elif btype == "text":
                        t = block.get("text", "").strip()
                        if t and not cls._RE_SYSTEM_TAGS.search(t):
                            user_text_parts.append(t)

                # Add user text if present (skip if message was only tool_results)
                if user_text_parts:
                    combined = "\n".join(user_text_parts)
                    # Skip if it looks like local command XML
                    if not cls._RE_LOCAL_STDOUT.search(
                        combined
                    ) and not cls._RE_COMMAND_NAME.search(combined):
                        result.append(
                            ParsedEntry(
                                role="user",
                                text=combined,
                                content_type="text",
                                timestamp=entry_timestamp,
                            )
                        )

        # Flush remaining pending tools at end.
        # In carry-over mode (monitor), keep them pending for the next call
        # without emitting entries. In one-shot mode (history), emit them.
        remaining_pending = dict[str, Any](pending_tools)
        if not _carry_over:
            for tool_id, tool_info in pending_tools.items():
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=tool_info.summary,
                        content_type="tool_use",
                        tool_use_id=tool_id,
                    )
                )

        # Strip whitespace
        for entry in result:
            entry.text = entry.text.strip()

        return result, remaining_pending
