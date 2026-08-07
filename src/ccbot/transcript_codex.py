"""Normalization of Codex rollout rows into Claude-shaped message blocks."""

import json
from typing import Any


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
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        if not isinstance(arguments, dict):
            arguments = {"input": arguments}
        return {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": str(payload.get("call_id") or payload.get("id") or ""),
                        "name": str(payload.get("name") or "tool"),
                        "input": arguments,
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
                        "content": payload.get("output") or "",
                    }
                ]
            },
        }
    return None
