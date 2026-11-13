"""OpenCode tool events must reach the same card renderer with their real data."""

from copy import deepcopy

import pytest

from ccbot.handlers.card_event_render import render_event
from ccbot.handlers.card_events import _apply_tool_result, _build_event
from ccbot.handlers.card_types import CardState
from ccbot.session_monitor import NewMessage
from ccbot.transcript_opencode import project_snapshot
from ccbot.transcript_parser import TranscriptParser


@pytest.mark.parametrize(
    "summary,visible",
    [
        ({"diffs": []}, True),
        ({"title": "request", "diffs": [{"file": "x"}]}, True),
        (True, False),
    ],
)
def test_user_diff_summary_is_visible_but_compaction_summary_is_hidden(
    summary, visible
):
    snapshot = {
        "session": {"id": "ses_display123", "directory": "/fixture"},
        "usage": [],
        "messages": [
            {
                "id": "user1",
                "role": "user",
                "time_created": 1000,
                "summary": summary,
                "parts": [
                    {"id": "text1", "type": "text", "text": "ACTUAL_USER_REQUEST"}
                ],
            }
        ],
    }
    original = deepcopy(snapshot)
    rows = project_snapshot(snapshot)
    assert snapshot == original
    parsed, _ = TranscriptParser.parse_entries(rows, pending_tools={})
    assert [x.text for x in parsed] == (["ACTUAL_USER_REQUEST"] if visible else [])


def render_tool(
    tool, arguments, output, metadata=None, status="completed", attachments=None
):
    snapshot = {
        "session": {"id": "ses_display123", "directory": "/fixture"},
        "usage": [],
        "messages": [
            {
                "id": "msg1",
                "role": "assistant",
                "time_created": 1000,
                "time": {"completed": 2000},
                "finish": "tool-calls",
                "parts": [
                    {
                        "id": "part1",
                        "type": "tool",
                        "tool": tool,
                        "callID": "call1",
                        "state": {
                            "status": status,
                            "input": arguments,
                            "output": output,
                            "metadata": metadata or {},
                            "attachments": attachments or [],
                            "time": {"start": 1000, "end": 2000},
                        },
                    }
                ],
            }
        ],
    }
    original = deepcopy(snapshot)
    projected = project_snapshot(snapshot)
    assert snapshot == original
    parsed, pending = TranscriptParser.parse_entries(projected, pending_tools={})
    assert not pending
    state = CardState()
    for entry in parsed:
        msg = NewMessage(
            session_id="ses_display123",
            text=entry.text,
            is_complete=bool(entry.stop_reason),
            content_type=entry.content_type,
            tool_use_id=entry.tool_use_id,
            tool_name=entry.tool_name,
            timestamp=entry.timestamp or "",
            is_error=entry.is_error,
            image_data=entry.image_data,
        )
        event = _build_event(msg)
        if entry.content_type == "tool_result":
            assert _apply_tool_result(state, event)
        else:
            state.events.append(event)
    assert len(state.events) == 1
    return state.events[0], render_event(
        state.events[0], in_flight=False, now=3, spoiler_line_limits=(20, 20)
    )


def test_read_displays_native_file_text_without_xml_envelope():
    event, rendered = render_tool(
        "read",
        {"filePath": "/fixture/sample.py"},
        "<path>/fixture/sample.py</path>\n<content>1: return 36</content>",
        {
            "display": {
                "type": "file",
                "text": "return 36",
                "lineStart": 1,
                "lineEnd": 1,
            }
        },
    )
    assert event.tool_name == "Read"
    assert event.tool_args == "/fixture/sample.py"
    assert event.tool_content == "return 36"
    assert "```python\nreturn 36" in rendered
    assert "<content>" not in rendered
    assert "\n- - -\n" in rendered


def test_apply_patch_keeps_complete_patch_and_native_diff():
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-"
        + "x" * 250
        + "\n+y\n*** End Patch"
    )
    diff = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old\n+new"
    event, rendered = render_tool(
        "apply_patch", {"patchText": patch}, "Success", {"diff": diff}
    )
    assert event.tool_name == "apply_patch"
    assert event.tool_args == patch
    assert event.tool_content == diff
    assert "```diff" in rendered


def test_completed_shell_with_nonzero_exit_is_an_error():
    event, rendered = render_tool(
        "bash", {"command": "python -c 'exit(7)'"}, "EXPECTED_FAILURE\n", {"exit": 7}
    )
    assert event.tool_name == "Bash"
    assert event.is_error
    assert rendered.startswith("\x02EXPHEADED_START\x02✗ Bash")
    assert "7" in rendered and "EXPECTED_FAILURE" in rendered


@pytest.mark.parametrize(
    ("native", "canonical", "arguments"),
    [
        ("webfetch", "WebFetch", {"url": "https://example.com"}),
        ("websearch", "WebSearch", {"query": "example"}),
        (
            "todowrite",
            "TodoWrite",
            {"todos": [{"content": "first", "status": "completed"}]},
        ),
        ("skill", "Skill", {"name": "fixture-skill"}),
        ("task", "Task", {"description": "fixture task", "prompt": "test"}),
        (
            "question",
            "AskUserQuestion",
            {"questions": [{"question": "Choose?", "options": []}]},
        ),
        ("mcp_fixture_tool", "mcp_fixture_tool", {"key": "value"}),
        ("codesearch", "CodeSearch", {"query": "example"}),
        ("todoread", "TodoRead", {}),
        ("lsp", "LSP", {"operation": "hover", "filePath": "sample.py", "line": 1}),
        ("list", "List", {"path": "/fixture"}),
        (
            "batch",
            "Batch",
            {"tool_calls": [{"tool": "read", "parameters": {"filePath": "a.py"}}]},
        ),
        ("invalid", "Invalid", {"tool": "missing", "error": "Unknown tool"}),
        ("plan_enter", "EnterPlanMode", {}),
        ("plan_exit", "ExitPlanMode", {}),
    ],
)
def test_native_names_and_results_are_preserved(native, canonical, arguments):
    event, rendered = render_tool(native, arguments, "NATIVE_RESULT")
    assert event.tool_name == canonical
    assert "NATIVE_RESULT" in rendered


def test_edit_camelcase_fields_produce_a_diff():
    event, rendered = render_tool(
        "edit",
        {"filePath": "/fixture/sample.py", "oldString": "old", "newString": "new"},
        "Success",
    )
    assert event.tool_name == "Edit"
    assert "-old" in rendered and "+new" in rendered


def test_write_keeps_written_text_and_language():
    event, rendered = render_tool(
        "write", {"filePath": "/fixture/sample.py", "content": "print(36)\n"}, "Success"
    )
    assert event.tool_name == "Write"
    assert event.tool_content == "print(36)"
    assert "```python\nprint(36)" in rendered


def test_custom_tool_keeps_nested_arguments_and_structured_result():
    event, rendered = render_tool(
        "mcp_fixture_tool",
        {"query": "x", "filters": {"status": ["ok"]}},
        {"rows": [{"value": 36}]},
    )
    assert '"filters"' in event.tool_args and '"status"' in event.tool_args
    assert '"value"' in event.tool_content or "36" in event.tool_content
    assert "36" in rendered


def test_native_truncation_remains_visible():
    _event, rendered = render_tool(
        "bash",
        {"command": "cat huge.txt"},
        "partial output",
        {"exit": 0, "truncated": True, "outputPath": "/fixture/full-output.txt"},
    )
    assert "Output truncated by OpenCode" in rendered
    assert "full-output.txt" in rendered


def test_user_abort_closes_tool_pair_without_provider_error():
    snapshot = {
        "session": {"id": "ses_abort123", "directory": "/fixture"},
        "usage": [],
        "messages": [
            {
                "id": "msg1",
                "role": "assistant",
                "time_created": 1000,
                "time": {"created": 1000, "completed": 2000},
                "error": {
                    "name": "MessageAbortedError",
                    "data": {"message": "Aborted"},
                },
                "parts": [
                    {
                        "id": "part1",
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "sleep 30"},
                            "output": "partial output\n<shell_metadata>User aborted the command</shell_metadata>",
                            "metadata": {"exit": None, "output": "partial output\n"},
                            "time": {"start": 1000, "end": 2000},
                        },
                    }
                ],
            }
        ],
    }
    parsed, pending = TranscriptParser.parse_entries(
        project_snapshot(snapshot), pending_tools={}
    )
    assert not pending
    assert not any(entry.api_error for entry in parsed)
    result = next(entry for entry in parsed if entry.content_type == "tool_result")
    assert "Interrupted" in result.text and "partial output" in result.text
    assert not result.is_error
    assert parsed[-1].text == "⏹ Interrupted" and parsed[-1].stop_reason == "end_turn"


def test_image_tool_result_keeps_original_image_bytes():
    event, _rendered = render_tool(
        "read",
        {"filePath": "/fixture/pixel.png"},
        "Image read successfully",
        attachments=[
            {"type": "file", "mime": "image/png", "url": "data:image/png;base64,eA=="}
        ],
    )
    assert event.image_data == [("image/png", b"x")]
