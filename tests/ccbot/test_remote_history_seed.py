from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_seed
from ccbot.handlers.card_seed import _ensure_seeded, _events_from_parsed_entries
from ccbot.handlers.card_types import CardState, Event
from ccbot.node_history import MAX_HISTORY_SEED_BYTES, _bound_entries
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.session_models import Session


def _transcript(path) -> None:
    rows = [
        {
            "type": "user",
            "message": {"role": "user", "content": "earlier question"},
            "timestamp": "2026-09-21T10:00:00Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "earlier answer"}],
                "stop_reason": "end_turn",
            },
            "timestamp": "2026-09-21T10:00:01Z",
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_worker_history_payload_has_a_hard_serialized_byte_limit() -> None:
    huge = "\\" * (MAX_HISTORY_SEED_BYTES + 1000)

    entries, truncated = _bound_entries(
        [
            {
                "role": huge,
                "text": huge,
                "content_type": "text",
                "tool_name": huge,
                "image_data": [{"media_type": "image/png", "data": huge}],
            }
        ]
    )

    assert truncated is True
    assert (
        len(json.dumps(entries, ensure_ascii=False).encode()) <= MAX_HISTORY_SEED_BYTES
    )


def test_remote_entries_use_the_local_tool_folding_model() -> None:
    sess = Session(id="remote", name="Remote", node_id="worker-a")

    events = _events_from_parsed_entries(
        sess,
        [
            {
                "role": "assistant",
                "text": "Read(file.py)",
                "content_type": "tool_use",
                "tool_use_id": "tool-1",
                "tool_name": "Read",
            },
            {
                "role": "user",
                "text": "Read(file.py) result",
                "content_type": "tool_result",
                "tool_use_id": "tool-1",
                "tool_name": "Read",
            },
        ],
        20,
    )

    assert len(events) == 1
    assert events[0].type == "tool_use"
    assert events[0].completed_at is not None


@pytest.mark.asyncio
async def test_worker_history_seed_is_bounded_and_versioned(
    monkeypatch, tmp_path
) -> None:
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript)
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    worker_session = SimpleNamespace(transcript_path=transcript)
    monkeypatch.setattr(
        executor, "_find_session", AsyncMock(return_value=worker_session)
    )
    monkeypatch.setattr(executor, "_bind_transcript", AsyncMock())

    first = await executor.seed_session_history(session_id="routing-1", max_turns=50)
    second = await executor.seed_session_history(
        session_id="routing-1",
        max_turns=50,
        known_version=first["version"],
    )

    assert [entry["text"] for entry in first["entries"]] == [
        "earlier question",
        "earlier answer",
    ]
    assert second == {
        "ok": True,
        "version": first["version"],
        "unchanged": True,
        "entries": [],
    }


@pytest.mark.asyncio
async def test_remote_empty_seed_retries_after_worker_version_changes(
    monkeypatch,
) -> None:
    runtime = SimpleNamespace(
        seed_session_history=AsyncMock(
            side_effect=[
                {"ok": True, "version": "1:1", "entries": []},
                {
                    "ok": True,
                    "version": "2:2",
                    "entries": [
                        {
                            "role": "assistant",
                            "text": "restored answer",
                            "content_type": "text",
                            "stop_reason": "end_turn",
                            "timestamp": "2026-09-21T10:00:01Z",
                        }
                    ],
                },
            ]
        )
    )
    monkeypatch.setattr(card_seed, "get_node_runtime", lambda _node: runtime)
    monkeypatch.setattr(
        card_seed.session_manager,
        "get_user_settings",
        lambda _uid: {"card_history": 20},
    )
    sess = Session(
        id="remote",
        name="Remote",
        node_id="worker-a",
        window_id="worker-a::@4",
        worker_session_id="routing-1",
    )
    state = CardState()

    await _ensure_seeded(42, sess, state)
    assert state.events == []
    assert state.seed_attempted is False
    await _ensure_seeded(42, sess, state)

    assert [event.text for event in state.events] == ["restored answer"]
    assert runtime.seed_session_history.await_args_list[1].kwargs == {
        "known_version": "1:1"
    }


@pytest.mark.asyncio
async def test_remote_offline_seed_retries_when_worker_reconnects(monkeypatch) -> None:
    runtime = SimpleNamespace(
        seed_session_history=AsyncMock(
            return_value={
                "ok": True,
                "version": "2:2",
                "entries": [
                    {
                        "role": "assistant",
                        "text": "available after reconnect",
                        "content_type": "text",
                        "stop_reason": "end_turn",
                    }
                ],
            }
        )
    )
    runtimes = iter((None, runtime))
    monkeypatch.setattr(card_seed, "get_node_runtime", lambda _node: next(runtimes))
    sess = Session(
        id="remote",
        name="Remote",
        node_id="worker-a",
        worker_session_id="routing-1",
    )
    state = CardState()

    await _ensure_seeded(42, sess, state)
    assert state.seed_attempted is False
    await _ensure_seeded(42, sess, state)

    assert [event.text for event in state.events] == ["available after reconnect"]


@pytest.mark.asyncio
async def test_remote_seed_preserves_live_event_arriving_during_rpc(
    monkeypatch,
) -> None:
    state = CardState()
    live = Event(type="text", text="live tail", body="live tail", started_at=3.0)

    async def seed(*_args, **_kwargs):
        state.events.append(live)
        return {
            "ok": True,
            "version": "2:2",
            "entries": [
                {
                    "role": "assistant",
                    "text": "history",
                    "content_type": "text",
                    "stop_reason": "end_turn",
                    "timestamp": "2026-09-21T10:00:01Z",
                }
            ],
        }

    runtime = SimpleNamespace(seed_session_history=AsyncMock(side_effect=seed))
    monkeypatch.setattr(card_seed, "get_node_runtime", lambda _node: runtime)
    sess = Session(
        id="remote",
        name="Remote",
        node_id="worker-a",
        window_id="worker-a::@4",
        worker_session_id="routing-1",
    )

    await _ensure_seeded(42, sess, state)

    assert [event.text for event in state.events] == ["history", "live tail"]
