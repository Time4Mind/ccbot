"""Basic JSONL and Claude-shaped message parsing helpers."""

import json
from typing import Any

from .transcript_types import ParsedMessage


def parse_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line, returning ``None`` for empty or invalid input."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def get_message_type(data: dict[str, Any]) -> str | None:
    """Get the top-level transcript message type."""
    return data.get("type")


def is_user_message(data: dict[str, Any]) -> bool:
    """Return whether this is a user message."""
    return data.get("type") == "user"


def extract_text_only(content_list: list[Any]) -> str:
    """Extract text blocks, excluding tool calls and thinking blocks."""
    if not isinstance(content_list, list):  # pyright: ignore[reportUnnecessaryIsInstance]
        if isinstance(content_list, str):
            return content_list
        return ""

    texts: list[str] = []
    for item in content_list:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if text:
                texts.append(text)
    return "\n".join(texts)


def parse_message(parser_cls: Any, data: dict[str, Any]) -> ParsedMessage | None:
    """Parse a Claude-shaped user/assistant row using ``parser_cls`` hooks."""
    msg_type = parser_cls.get_message_type(data)
    if msg_type not in ("user", "assistant"):
        return None

    message = data.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content", "")
    if isinstance(content, list):
        text = parser_cls.extract_text_only(content)
    else:
        text = str(content) if content else ""
    text = parser_cls._RE_ANSI_ESCAPE.sub("", text)

    if msg_type == "user" and text:
        stdout_match = parser_cls._RE_LOCAL_STDOUT.search(text)
        if stdout_match:
            stdout = stdout_match.group(1).strip()
            cmd_match = parser_cls._RE_COMMAND_NAME.search(text)
            cmd = cmd_match.group(1) if cmd_match else None
            return ParsedMessage(
                message_type="local_command",
                text=stdout,
                tool_name=cmd,
            )
        cmd_match = parser_cls._RE_COMMAND_NAME.search(text)
        if cmd_match:
            return ParsedMessage(
                message_type="local_command_invoke",
                text="",
                tool_name=cmd_match.group(1),
            )

    return ParsedMessage(message_type=msg_type, text=text)


def get_timestamp(data: dict[str, Any]) -> str | None:
    """Extract timestamp from message data."""
    return data.get("timestamp")
