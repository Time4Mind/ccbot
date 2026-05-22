"""Tests for ``_ensure_seeded`` / ``_seed_events_from_jsonl`` — pulls
recent JSONL turns into state.events on first card access after a bot
restart so the user sees history, not a 1/1 empty page."""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from ccbot.handlers.notifications import (
    CardState,
    _ensure_seeded,
    _seed_events_from_jsonl,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.mark.asyncio
class TestSeedFromJsonl:
    async def test_no_window_returns_empty(self, monkeypatch) -> None:
        from ccbot.session import Session

        sess = Session(id="x", name="y")  # no window_id
        events = await _seed_events_from_jsonl(sess)
        assert events == []

    async def test_missing_session_returns_empty(self, monkeypatch) -> None:
        from ccbot.session import Session, session_manager

        # Window has no session_id/cwd → no transcript path → empty seed.
        ws = session_manager.get_window_state("@seed-missing")
        ws.session_id = ""
        ws.cwd = ""
        sess = Session(id="x", name="y", window_id="@seed-missing")
        events = await _seed_events_from_jsonl(sess)
        assert events == []

    async def test_pulls_recent_end_turns(self, tmp_path: Path, monkeypatch) -> None:
        import ccbot.session_claude_io as scio
        from ccbot.session import Session, session_manager

        jsonl = tmp_path / "session.jsonl"
        # Simulate: user message → assistant text with end_turn.
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "hello",
                    },
                    "timestamp": "2026-05-15T09:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "world"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                    "timestamp": "2026-05-15T09:00:01Z",
                },
            ],
        )

        ws = session_manager.get_window_state("@seed-turns")
        ws.session_id = "sess-uuid"
        ws.cwd = "/some/dir"
        monkeypatch.setattr(scio, "build_session_file_path", lambda _sid, _cwd: jsonl)
        sess = Session(id="x", name="y", window_id="@seed-turns")
        events = await _seed_events_from_jsonl(sess)
        # Got at least the assistant final_text from end_turn.
        assert len(events) >= 1
        types = {ev.type for ev in events}
        assert "final_text" in types


@pytest.mark.asyncio
class TestEnsureSeededIdempotent:
    async def test_no_op_when_events_present(self, monkeypatch) -> None:
        import ccbot.session_claude_io as scio
        from ccbot.handlers.notifications import Event
        from ccbot.session import Session

        state = CardState()
        state.events.append(Event(type="user_msg", text="👤 hi", started_at=1.0))
        called = {"path": 0}

        def _bp(_sid, _cwd):
            called["path"] += 1
            return None

        monkeypatch.setattr(scio, "build_session_file_path", _bp)
        sess = Session(id="x", name="y", window_id="@1")
        await _ensure_seeded(1, sess, state)
        assert called["path"] == 0  # no JSONL read because events present
        assert len(state.events) == 1  # untouched

    async def test_seed_attempted_only_once(self, monkeypatch) -> None:
        import ccbot.session_claude_io as scio
        from ccbot.session import Session, session_manager

        ws = session_manager.get_window_state("@seed-once")
        ws.session_id = "sess-uuid"
        ws.cwd = "/some/dir"
        called = {"path": 0}

        def _bp(_sid, _cwd):
            called["path"] += 1
            return Path("/nonexistent-seed.jsonl")  # not exists → empty seed

        monkeypatch.setattr(scio, "build_session_file_path", _bp)
        state = CardState()
        sess = Session(id="x", name="y", window_id="@seed-once")
        await _ensure_seeded(1, sess, state)
        await _ensure_seeded(1, sess, state)
        await _ensure_seeded(1, sess, state)
        # Even with three calls, the path resolver fires exactly once —
        # guarded by ``state.seed_attempted``.
        assert called["path"] == 1
