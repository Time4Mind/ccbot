"""Normalization of Codex rollout rows into Claude-shaped message blocks."""

import json
import re
from typing import Any


_INJECTED_USER_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<turn_aborted>",
    "<recommended_plugins>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<plugins_instructions>",
    "<skills_instructions>",
    "<subagent_notification>",
)

_HARNESS_TOOL_MARKERS = (
    "/agents.md",
    ".agents/skills/",
    ".codex/memories/",
    "/doc/local/project-memory.md",
    ".claude/claude.md",
)

_EXEC_COMMAND_RE = re.compile(
    r"\btools\.exec_command\s*\(\s*\{[\s\S]*?[\"']?\bcmd\b[\"']?\s*:\s*"
)
_COMPLETED_OUTPUT_RE = re.compile(
    r"\AScript completed\nWall time ([0-9.]+) seconds\nOutput:\n?([\s\S]*)\Z"
)


def _extract_exec_command(source: str) -> str | None:
    """Extract the actual shell command from a Codex JS exec wrapper."""
    match = _EXEC_COMMAND_RE.search(source)
    if match is None:
        return None
    encoded = source[match.end() :].lstrip()
    if not encoded.startswith('"'):
        return None
    try:
        command, _end = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, str) else None


def _structure_tool_output(value: Any) -> Any:
    """Turn the stable Codex execution envelope into readable fields."""
    if not isinstance(value, list):
        return value
    text_parts = [
        str(item.get("text") or "")
        for item in value
        if isinstance(item, dict)
        and item.get("type") in ("text", "input_text", "output_text")
    ]
    if not text_parts:
        return value
    raw = "".join(text_parts)
    match = _COMPLETED_OUTPUT_RE.match(raw)
    if match is None:
        return value
    duration, output = match.groups()
    lines = ["status: completed", f"duration: {duration} s"]
    if output.strip():
        lines.extend(("output:", output.rstrip()))
    else:
        lines.append("output: no output")
    non_text = [
        item
        for item in value
        if not (
            isinstance(item, dict)
            and item.get("type") in ("text", "input_text", "output_text")
        )
    ]
    return [{"type": "text", "text": "\n".join(lines)}, *non_text]


def is_injected_user_text(text: str) -> bool:
    """Return whether Codex labelled harness context as a user message."""
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _INJECTED_USER_PREFIXES)


def is_harness_tool_input(value: Any) -> bool:
    """Identify Codex tool inputs used only to load agent-side instructions."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str).casefold()
    except (TypeError, ValueError):
        serialized = str(value).casefold()
    return any(marker in serialized for marker in _HARNESS_TOOL_MARKERS)


def normalize_codex_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a stable subset of Codex rollout events to Claude blocks.

    Codex emits user/agent text as ``event_msg`` rows and tool calls as
    ``response_item`` rows. Normalizing at this boundary lets the existing
    history, live-card, tool-pairing, and Telegram formatting pipeline stay
    unchanged.
    """
    top_type = data.get("type")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    timestamp = data.get("timestamp")
    if top_type == "event_msg":
        event_type = payload.get("type")
        if event_type == "user_message":
            text = str(payload.get("message") or "")
            if is_injected_user_text(text):
                return None
            return {
                "type": "user",
                "timestamp": timestamp,
                "message": {"content": [{"type": "text", "text": text}]},
            }
        if event_type == "agent_message":
            text = str(payload.get("message") or "")
            phase = str(payload.get("phase") or "")
            return {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn"
                    if phase in ("final_answer", "final")
                    else None,
                },
            }
        return None
    if top_type != "response_item":
        return None
    item_type = payload.get("type")
    # Codex 0.147 stopped emitting the duplicate event_msg rows that used
    # to carry user and assistant text. Its replacement message rows are
    # numbered with an ordinal. Codex 0.146 also wrote unnumbered message
    # response_items alongside event_msg rows, so accepting only numbered
    # rows here preserves the old fallback without rendering every turn twice.
    if item_type == "message" and data.get("ordinal") is not None:
        role = str(payload.get("role") or "")
        if role not in ("user", "assistant"):
            return None
        raw_content = payload.get("content", "")
        content: list[dict[str, str]] = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type not in ("input_text", "output_text", "text"):
                    continue
                text = str(block.get("text") or "")
                if text:
                    content.append({"type": "text", "text": text})
        elif isinstance(raw_content, str) and raw_content:
            content.append({"type": "text", "text": raw_content})
        if not content:
            return None
        if role == "user" and any(
            is_injected_user_text(block["text"]) for block in content
        ):
            return None
        phase = str(payload.get("phase") or "")
        return {
            "type": role,
            "timestamp": timestamp,
            "message": {
                "content": content,
                "stop_reason": "end_turn"
                if role == "assistant" and phase in ("final_answer", "final")
                else None,
            },
        }
    if item_type in ("function_call", "custom_tool_call"):
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = payload.get("input")
        raw_arguments = arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        if not isinstance(arguments, dict):
            arguments = {"input": arguments}
        hidden = is_harness_tool_input(raw_arguments)
        name = str(payload.get("name") or "tool")
        if name == "exec" and isinstance(raw_arguments, str):
            command = _extract_exec_command(raw_arguments)
            if command is not None:
                name = "Bash"
                arguments = {"command": command}
        return {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": str(payload.get("call_id") or payload.get("id") or ""),
                        "name": name,
                        "input": arguments,
                        "_ccbot_hidden": hidden,
                    }
                ],
                "stop_reason": "tool_use",
            },
        }
    if item_type in ("function_call_output", "custom_tool_call_output"):
        return {
            "type": "user",
            "timestamp": timestamp,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(
                            payload.get("call_id") or payload.get("id") or ""
                        ),
                        "content": _structure_tool_output(payload.get("output") or ""),
                    }
                ]
            },
        }
    return None
