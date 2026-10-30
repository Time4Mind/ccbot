"""Project visible ZCode messages and tool states into the shared transcript contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _row(
    snapshot: dict[str, Any],
    key: str,
    role: str,
    content: list[dict[str, Any]],
    *,
    when: int = 0,
    stop: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    session = snapshot["session"]
    return {
        "type": role,
        "uuid": key,
        "sessionId": session["id"],
        "cwd": session["directory"],
        "timestamp": datetime.fromtimestamp(when / 1000, timezone.utc).isoformat(),
        "message": {"role": role, "content": content, "stop_reason": stop},
        **({"isApiErrorMessage": True, "error": error} if error else {}),
    }


def project_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    usage_by_message = {u.get("assistant_message_id"): u for u in snapshot["usage"]}
    for message in snapshot["messages"]:
        semantics = message.get("semantics") or {}
        metadata = message.get("metadata") or {}
        if (
            semantics.get("uiVisibility") == "hidden"
            or semantics.get("transcriptVisibility") == "hidden"
            or metadata.get("visibility") == "model-only"
            or message.get("visibility") == "model-only"
            or message.get("summary")
            or (message.get("role") == "user" and message.get("synthetic"))
        ):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        when = message["time_created"]
        completed = bool((message.get("time") or {}).get("completed"))
        texts: list[dict[str, Any]] = []
        for part in message["parts"]:
            kind = part.get("type")
            identity = part["id"]
            if part.get("ignored") or part.get("synthetic"):
                continue
            if kind == "tool":
                state = part.get("state") or {}
                if state.get("status") == "pending":
                    continue
                call_id = str(part.get("callID") or identity)
                rows.append(
                    _row(
                        snapshot,
                        identity + ":call",
                        "assistant",
                        [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": part.get("tool", "Tool"),
                                "input": state.get("input") or {},
                            }
                        ],
                        when=when,
                    )
                )
                if state.get("status") in ("completed", "error"):
                    content: list[dict[str, Any]] = [
                        {
                            "type": "text",
                            "text": str(
                                state.get("output") or state.get("error") or ""
                            ),
                        }
                    ]
                    for attachment in state.get("attachments") or []:
                        url = str(attachment.get("url") or "")
                        if url.startswith("data:image/") and ";base64," in url:
                            mime, data = url[5:].split(";base64,", 1)
                            content.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime,
                                        "data": data,
                                    },
                                }
                            )
                    rows.append(
                        _row(
                            snapshot,
                            identity + ":result",
                            "user",
                            [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": call_id,
                                    "content": content,
                                    "is_error": state["status"] == "error",
                                }
                            ],
                            when=int((state.get("time") or {}).get("end") or when),
                        )
                    )
            elif kind in ("text", "reasoning"):
                if (
                    role == "assistant"
                    and not completed
                    and not (part.get("time") or {}).get("end")
                ):
                    continue
                text = str(part.get("text") or "")
                if not text:
                    continue
                if kind == "reasoning":
                    rows.append(
                        _row(
                            snapshot,
                            identity,
                            "assistant",
                            [{"type": "thinking", "thinking": text}],
                            when=when,
                        )
                    )
                else:
                    texts.append({"type": "text", "text": text})
        if texts:
            stop = None
            if role == "assistant" and completed:
                stop = (
                    "tool_use"
                    if message.get("finish") in ("tool-calls", "tool_use")
                    else "end_turn"
                )
            # Completed assistant text is emitted as one message so history and
            # final delivery share the exact same boundary and do not duplicate it.
            if role == "user" or completed:
                row = _row(
                    snapshot, message["id"] + ":text", role, texts, when=when, stop=stop
                )
                row["message"]["model"] = message.get("modelId", "")
                rows.append(row)
        error = message.get("error")
        if error:
            usage = usage_by_message.get(message["id"], {})
            details = error.get("data") or {}
            status = details.get("statusCode", details.get("status"))
            code = (
                "authentication_failed"
                if status in (401, 403)
                or usage.get("error_code")
                in ("401", "1000", "1001", "1002", "1003", "1004")
                else "zcode_request_failed"
            )
            text = str(
                details.get("message") or error.get("name") or "ZCode request failed"
            )
            rows.append(
                _row(
                    snapshot,
                    message["id"] + ":error",
                    "assistant",
                    [{"type": "text", "text": text}],
                    when=when,
                    stop="end_turn",
                    error=code,
                )
            )
    for usage in snapshot["usage"]:
        if usage.get("status") not in ("completed", "failed", "error", "cancelled"):
            continue
        rows.append(
            {
                "type": "zcode_usage",
                "uuid": str(usage["id"]) + ":" + str(usage.get("status")),
                "sessionId": snapshot["session"]["id"],
                "usage": usage,
            }
        )
    return rows
