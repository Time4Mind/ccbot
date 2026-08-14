"""Regression coverage for append-only live-history prewarming."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ccbot.handlers import history
from ccbot.handlers import history_incremental


def _assistant(text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-12T12:34:56Z",
                "message": {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                },
            }
        ).encode()
        + b"\n"
    )


def _tool_use(tool_id: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Read",
                            "input": {"file_path": "/tmp/example.py"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            }
        ).encode()
        + b"\n"
    )


def _tool_result(tool_id: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "file body",
                        }
                    ]
                },
            }
        ).encode()
        + b"\n"
    )


@pytest.fixture(autouse=True)
def _clear_history_globals():
    history._pages_cache.clear()
    history._incremental_history.clear()
    history._prewarm_locks.clear()
    history._prewarm_tasks.clear()
    history._last_prewarm_attempt.clear()
    yield
    history._pages_cache.clear()
    history._incremental_history.clear()
    history._prewarm_locks.clear()
    history._prewarm_tasks.clear()
    history._last_prewarm_attempt.clear()


def _point_window_at(monkeypatch: pytest.MonkeyPatch, transcript: Path) -> None:
    monkeypatch.setattr(history, "_window_file_path", lambda _wid: transcript)
    monkeypatch.setattr(
        history.session_manager, "get_display_name", lambda _wid: "incremental"
    )


@pytest.mark.asyncio
async def test_append_reads_only_from_previous_complete_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    first = _assistant("first answer")
    transcript.write_bytes(first)
    _point_window_at(monkeypatch, transcript)

    offsets: list[int] = []
    real_reader = history_incremental.read_history_delta

    def recording_reader(path, offset, pending):
        offsets.append(offset)
        return real_reader(path, offset, pending)

    monkeypatch.setattr(history, "read_history_delta", recording_reader)

    assert await history.prewarm_pages_cache("@1") is True
    with transcript.open("ab") as stream:
        stream.write(_assistant("second answer"))
    assert await history.prewarm_pages_cache("@1") is True

    assert offsets == [0, len(first)]
    state = history._incremental_history["@1"]
    assert state.rendered_total == 2
    assert not hasattr(state, "messages")
    rendered = "\n".join(history._pages_cache["@1"][2])
    assert "first answer" in rendered
    assert "second answer" in rendered
    assert history._pages_cache["@1"][3] == 2


@pytest.mark.asyncio
async def test_concurrent_prewarm_performs_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_assistant("only answer"))
    _point_window_at(monkeypatch, transcript)

    calls = 0
    real_reader = history_incremental.read_history_delta

    def slow_reader(path, offset, pending):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return real_reader(path, offset, pending)

    monkeypatch.setattr(history, "read_history_delta", slow_reader)

    results = await asyncio.gather(
        history.prewarm_pages_cache("@1"),
        history.prewarm_pages_cache("@1"),
        history.prewarm_pages_cache("@1"),
    )

    assert calls == 1
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_truncation_discards_old_incremental_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_assistant("old one") + _assistant("old two"))
    _point_window_at(monkeypatch, transcript)

    offsets: list[int] = []
    real_reader = history_incremental.read_history_delta

    def recording_reader(path, offset, pending):
        offsets.append(offset)
        return real_reader(path, offset, pending)

    monkeypatch.setattr(history, "read_history_delta", recording_reader)

    await history.prewarm_pages_cache("@1")
    transcript.write_bytes(_assistant("new"))
    await history.prewarm_pages_cache("@1")

    assert offsets == [0, 0]
    assert history._incremental_history["@1"].rendered_total == 1
    rendered = "\n".join(history._pages_cache["@1"][2])
    assert "new" in rendered
    assert "old one" not in rendered
    assert "old two" not in rendered
    assert history._pages_cache["@1"][3] == 1


@pytest.mark.asyncio
async def test_truncate_and_regrow_past_old_offset_still_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fast rewrite can be larger than the old offset by the next poll."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_assistant("old one") + _assistant("old two"))
    _point_window_at(monkeypatch, transcript)
    await history.prewarm_pages_cache("@1")
    old_offset = history._incremental_history["@1"].offset

    replacement = b"".join(_assistant(f"replacement {index}") for index in range(4))
    assert len(replacement) > old_offset
    transcript.write_bytes(replacement)
    await history.prewarm_pages_cache("@1")

    state = history._incremental_history["@1"]
    assert state.rendered_total == 4
    rendered = "\n".join(history._pages_cache["@1"][2])
    assert "old one" not in rendered
    assert "old two" not in rendered
    for index in range(4):
        assert f"replacement {index}" in rendered


@pytest.mark.asyncio
async def test_partial_tail_waits_for_completion_without_busy_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    first = _assistant("complete")
    second = _assistant("eventually complete")
    transcript.write_bytes(first + second[:-2])
    _point_window_at(monkeypatch, transcript)

    calls = 0
    real_reader = history_incremental.read_history_delta

    def recording_reader(path, offset, pending):
        nonlocal calls
        calls += 1
        return real_reader(path, offset, pending)

    monkeypatch.setattr(history, "read_history_delta", recording_reader)

    await history.prewarm_pages_cache("@1")
    assert history._pages_cache["@1"][3] == 1
    assert history._incremental_history["@1"].offset == len(first)

    # Unchanged incomplete data is a cache hit, not another parse attempt.
    assert await history.prewarm_pages_cache("@1") is False
    assert calls == 1

    with transcript.open("ab") as stream:
        stream.write(second[-2:])
    await history.prewarm_pages_cache("@1")
    assert calls == 2
    assert history._pages_cache["@1"][3] == 2


@pytest.mark.asyncio
async def test_tool_pairing_state_survives_separate_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_tool_use("tool-1"))
    _point_window_at(monkeypatch, transcript)

    await history.prewarm_pages_cache("@1")
    state = history._incremental_history["@1"]
    assert "tool-1" in state.pending_tools
    assert "@1" not in history._pages_cache  # bare tool_use is hidden

    with transcript.open("ab") as stream:
        stream.write(_tool_result("tool-1"))
    await history.prewarm_pages_cache("@1")

    state = history._incremental_history["@1"]
    assert state.pending_tools == {}
    assert history._pages_cache["@1"][3] == 1
    assert "Read" in history._pages_cache["@1"][2][0]


@pytest.mark.asyncio
async def test_append_reflows_only_the_previous_last_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(
        b"".join(_assistant(f"old-{index}-" + "x" * 500) for index in range(40))
    )
    _point_window_at(monkeypatch, transcript)
    await history.prewarm_pages_cache("@1")

    old_pages = list(history._pages_cache["@1"][2])
    assert len(old_pages) > 3
    split_input_lengths: list[int] = []
    real_split = history.split_message

    def recording_split(text: str, max_length: int = 4096):
        split_input_lengths.append(len(text))
        return real_split(text, max_length=max_length)

    monkeypatch.setattr(history, "split_message", recording_split)
    with transcript.open("ab") as stream:
        stream.write(_assistant("new-tail"))
    await history.prewarm_pages_cache("@1")

    new_pages = history._pages_cache["@1"][2]
    # Page zero changes only because its exact total is refreshed; every
    # completed middle page remains byte-identical.
    assert new_pages[1:-1] == old_pages[1:-1]
    assert split_input_lengths
    assert max(split_input_lengths) < 5000
    assert "new-tail" in new_pages[-1]
    assert history._incremental_history["@1"].rendered_total == 41


def test_append_helper_keeps_every_page_within_telegram_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        history.session_manager, "get_display_name", lambda _wid: "incremental"
    )
    messages = [
        {
            "role": "assistant",
            "text": f"message-{index}-" + "x" * 700,
            "content_type": "text",
            "timestamp": "2026-08-12T12:34:56Z",
        }
        for index in range(100)
    ]
    pages: list[str] = []
    total = 0
    for message in messages:
        pages, total, visible_added = history._append_cached_pages(
            "@1", pages, total, [message]
        )
        assert visible_added == 1
        assert all(len(page) <= 4096 for page in pages)

    rendered = "\n".join(pages)
    assert total == 100
    for index in range(100):
        assert f"message-{index}-" in rendered
