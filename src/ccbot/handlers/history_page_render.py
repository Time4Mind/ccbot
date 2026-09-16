"""Pure formatting helpers for paginated live-session history."""

from __future__ import annotations

from typing import Any

from ..config import config
from ..session import session_manager
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser


def visible_history_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the stable visibility policy used by the full-history view."""
    visible = messages
    if not config.show_user_messages:
        visible = [message for message in visible if message["role"] == "assistant"]
    return [message for message in visible if message.get("content_type") != "tool_use"]


def history_header(window_id: str, total: int) -> str:
    display_name = session_manager.get_display_name(window_id)
    return f"📋 [{display_name}] Messages ({total} total)"


def history_message_blocks(messages: list[dict[str, Any]]) -> list[str]:
    """Format only message bodies; page/header assembly is handled separately."""
    quote_start = TranscriptParser.EXPANDABLE_QUOTE_START
    quote_end = TranscriptParser.EXPANDABLE_QUOTE_END
    blocks: list[str] = []
    for message in messages:
        timestamp = message.get("timestamp")
        hh_mm = ""
        if timestamp:
            try:
                time_part = timestamp.split("T")[1] if "T" in timestamp else timestamp
                hh_mm = time_part[:5]
            except (IndexError, TypeError):
                hh_mm = ""
        separator = f"───── {hh_mm} ─────" if hh_mm else "─────────────"
        text = message["text"].replace(quote_start, "").replace(quote_end, "")
        fence_lines = sum(
            1 for line in text.split("\n") if line.strip().startswith("```")
        )
        if fence_lines % 2 == 1:
            text += "\n```"
        role = message.get("role", "assistant")
        content_type = message.get("content_type", "text")
        if role == "user":
            body = f"👤 {text}"
        elif content_type == "thinking":
            body = f"∴ Thinking…\n{text}"
        else:
            body = text
        blocks.append(f"{separator}\n\n{body}")
    return blocks


def render_cached_pages(
    window_id: str, messages: list[dict[str, Any]]
) -> tuple[list[str], int]:
    """Render an initial/rebuilt transcript snapshot into Telegram pages."""
    visible = visible_history_messages(messages)
    total = len(visible)
    if total == 0:
        return [], 0
    text = "\n\n".join(
        [history_header(window_id, total), *history_message_blocks(visible)]
    )
    return list(split_message(text, max_length=4096)), total
