"""Adapt native OpenCode tool data to CCBot's shared card presentation."""

from copy import deepcopy
import json
from typing import Any

from .transcript_format import format_edit_diff

TOOL_NAMES = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "list": "List",
    "task": "Task",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "codesearch": "CodeSearch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "skill": "Skill",
    "question": "AskUserQuestion",
    "lsp": "LSP",
    "plan_enter": "EnterPlanMode",
    "plan_exit": "ExitPlanMode",
    "apply_patch": "apply_patch",
    "batch": "Batch",
    "invalid": "Invalid",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, indent=2)
    )


def normalize_tool(part: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(part)
    native = str(part.get("tool") or "Tool")
    name = TOOL_NAMES.get(native, native)
    result["tool"] = name
    state = result.setdefault("state", {})
    args = state.get("input") or {}
    if not isinstance(args, dict):
        args = {"input": args}
    for old, new in (
        ("filePath", "file_path"),
        ("oldString", "old_string"),
        ("newString", "new_string"),
    ):
        if old in args:
            args[new] = args[old]
    if native == "skill" and "name" in args:
        args["skill"] = args["name"]
    state["input"] = args
    metadata = state.get("metadata") or {}
    output = _text(state.get("output") or state.get("error"))
    summary = "Error" if state.get("status") == "error" else "Completed"
    raw_args = part.get("state", {}).get("input") or {}
    if name == "Bash":
        shown_args = str(args.get("command") or "")
        exit_code = metadata.get("exit")
        if isinstance(exit_code, int):
            summary = f"exit code: {exit_code}"
            if exit_code != 0:
                state["status"] = "error"
    elif name in ("Read", "Write", "Edit"):
        shown_args = str(args.get("file_path") or "")
        extra = {
            key: value
            for key, value in raw_args.items()
            if key
            not in {
                "filePath",
                "file_path",
                "content",
                "oldString",
                "newString",
                "old_string",
                "new_string",
            }
        }
        if extra:
            shown_args += "\n" + _text(extra)
        if name == "Read" and isinstance(metadata.get("display"), dict):
            display = metadata["display"]
            if isinstance(display.get("text"), str):
                output = display["text"]
        elif name == "Write" and isinstance(args.get("content"), str):
            output = args["content"]
        elif name == "Edit":
            output = (
                _text(metadata.get("diff"))
                or format_edit_diff(
                    str(args.get("old_string") or ""), str(args.get("new_string") or "")
                )
                or output
            )
        if state.get("status") != "error":
            lines = len(output.splitlines())
            summary = f"{'Read' if name == 'Read' else 'Wrote' if name == 'Write' else 'Updated'} {lines} lines"
    elif name == "apply_patch":
        shown_args = str(args.get("patchText") or args.get("input") or "")
        output = _text(metadata.get("diff")) or output
        files = metadata.get("files")
        if isinstance(files, list):
            summary = f"Updated {len(files)} files"
    else:
        shown_args = _text(raw_args)
    if metadata.get("truncated"):
        output += "\n[Output truncated by OpenCode]"
        if metadata.get("outputPath"):
            output += "\n" + str(metadata["outputPath"])
    result["_ccbot_args"] = shown_args
    result["_ccbot_display"] = {"summary": summary, "text": output or "no output"}
    return result
