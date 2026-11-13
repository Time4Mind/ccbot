"""Project the common native message/part shape while retaining OpenCode identity."""

from typing import Any
from copy import deepcopy
from datetime import datetime, timezone

from .opencode_tools import normalize_tool
from .transcript_codex import is_harness_tool_input
from .transcript_zcode import project_snapshot as project_native_snapshot


def project_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = deepcopy(snapshot)
    tools = {}
    aborted_messages = set()
    for message in normalized["messages"]:
        # OpenCode uses summary objects for visible user-turn file diffs;
        # ZCode's boolean summary marker instead denotes hidden compaction.
        if isinstance(message.get("summary"), dict):
            message.pop("summary")
        aborted = (message.get("error") or {}).get("name") == "MessageAbortedError"
        if aborted:
            aborted_messages.add(message["id"] + ":error")
        for index, part in enumerate(message["parts"]):
            if part.get("type") == "tool":
                part = normalize_tool(part)
                state = part["state"]
                metadata = state.get("metadata") or {}
                interrupted = aborted and (
                    state.get("status") in ("pending", "running", "error")
                    or (
                        part["tool"] == "Bash"
                        and "exit" in metadata
                        and metadata["exit"] is None
                    )
                )
                if interrupted:
                    state["status"] = "completed"
                    state["output"] = "[Request interrupted by user for tool use]"
                    part["_ccbot_display"] = {
                        "summary": "Interrupted",
                        "text": str(metadata.get("output") or ""),
                    }
                message["parts"][index] = part
                tools[part["id"]] = part
    rows = project_native_snapshot(normalized)
    for message in normalized["messages"]:
        if (message.get("time") or {}).get("completed") and any(
            part.get("type") == "tool"
            and (part.get("state") or {}).get("error")
            == "The user rejected permission to use this specific tool call."
            for part in message["parts"]
        ):
            identities = {part["id"] for part in message["parts"]} | {message["id"]}
            end = max(
                (
                    index
                    for index, row in enumerate(rows)
                    if str(row.get("uuid", "")).split(":")[0] in identities
                ),
                default=-1,
            )
            rows.insert(
                end + 1,
                {
                    "type": "assistant",
                    "uuid": message["id"] + ":permission-denied",
                    "sessionId": normalized["session"]["id"],
                    "cwd": normalized["session"]["directory"],
                    "timestamp": datetime.fromtimestamp(
                        message["time"]["completed"] / 1000, timezone.utc
                    ).isoformat(),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "⏹ Permission denied"}],
                        "stop_reason": "end_turn",
                    },
                },
            )
    for row in rows:
        if row.get("uuid") in aborted_messages:
            row.pop("isApiErrorMessage", None)
            row.pop("error", None)
            row["message"]["content"] = [{"type": "text", "text": "⏹ Interrupted"}]
        if row.get("type") == "zcode_usage":
            row["type"] = "opencode_usage"
        if row.get("error") == "zcode_request_failed":
            row["error"] = "opencode_request_failed"
        identity, separator, phase = str(row.get("uuid") or "").rpartition(":")
        if separator and identity in tools:
            tool = tools[identity]
            block = row["message"]["content"][0]
            if phase == "call":
                block["_ccbot_args"] = tool["_ccbot_args"]
                block["_ccbot_hidden"] = is_harness_tool_input(tool["state"]["input"])
            elif phase == "result":
                block["_ccbot_display"] = tool["_ccbot_display"]
                block["_ccbot_tool"] = {
                    "name": tool["tool"],
                    "args": tool["_ccbot_args"],
                    "hidden": is_harness_tool_input(tool["state"]["input"]),
                }
    return rows
