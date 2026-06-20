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

    async def test_successful_seed_latches(self, tmp_path: Path, monkeypatch) -> None:
        # A non-empty seed sets ``seed_attempted`` so later calls short-out.
        import ccbot.session_claude_io as scio
        from ccbot.session import Session, session_manager

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "hi"},
                    "timestamp": "2026-05-15T09:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    "timestamp": "2026-05-15T09:00:01Z",
                },
            ],
        )
        ws = session_manager.get_window_state("@seed-latch")
        ws.session_id = "sess-uuid"
        ws.cwd = "/some/dir"
        monkeypatch.setattr(scio, "build_session_file_path", lambda _s, _c: jsonl)
        state = CardState()
        sess = Session(id="x", name="y", window_id="@seed-latch")
        await _ensure_seeded(1, sess, state)
        assert len(state.events) >= 1
        assert state.seed_attempted is True

    async def test_empty_seed_not_latched_retries_when_transcript_lands(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Regression: a restored (``claude --resume``) session builds its
        # card before claude has flushed the resumed transcript. The early
        # read returns [] — it must NOT latch ``seed_attempted``, so that a
        # later event (once the transcript is on disk) seeds the history.
        import ccbot.session_claude_io as scio
        from ccbot.session import Session, session_manager

        ws = session_manager.get_window_state("@seed-restore")
        ws.session_id = "sess-uuid"
        ws.cwd = "/some/dir"
        jsonl = tmp_path / "resumed.jsonl"  # not flushed yet
        monkeypatch.setattr(scio, "build_session_file_path", lambda _s, _c: jsonl)
        state = CardState()
        sess = Session(id="x", name="y", window_id="@seed-restore")

        # 1) transcript missing → empty seed, not latched.
        await _ensure_seeded(1, sess, state)
        assert state.events == []
        assert state.seed_attempted is False

        # 2) claude flushes the resumed transcript.
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "earlier turn"},
                    "timestamp": "2026-05-15T09:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "earlier reply"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                    "timestamp": "2026-05-15T09:00:01Z",
                },
            ],
        )

        # 3) next event re-seeds (mtime advanced) → history lands + latches.
        await _ensure_seeded(1, sess, state)
        assert len(state.events) >= 1
        assert state.seed_attempted is True

    async def test_unchanged_empty_transcript_not_reparsed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # An existing but content-less transcript yields []; the mtime gate
        # must suppress re-parsing it on every event until it changes.
        import ccbot.handlers.notifications as notif
        import ccbot.session_claude_io as scio
        from ccbot.session import Session, session_manager

        ws = session_manager.get_window_state("@seed-gate")
        ws.session_id = "sess-uuid"
        ws.cwd = "/some/dir"
        f = tmp_path / "empty.jsonl"
        f.write_text("")  # exists, empty → empty seed
        monkeypatch.setattr(scio, "build_session_file_path", lambda _s, _c: f)
        calls = {"n": 0}

        async def _spy(_sess, max_turns=0):
            calls["n"] += 1
            return []

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _spy)
        state = CardState()
        sess = Session(id="x", name="y", window_id="@seed-gate")
        await _ensure_seeded(1, sess, state)
        await _ensure_seeded(1, sess, state)
        await _ensure_seeded(1, sess, state)
        # mtime never advanced → parsed exactly once; never latched.
        assert calls["n"] == 1
        assert state.seed_attempted is False
