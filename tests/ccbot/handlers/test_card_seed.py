"""Tests for ``_ensure_seeded`` / ``_seed_events_from_jsonl`` — pulls
recent JSONL turns into state.events on first card access after a bot
restart so the user sees history, not a 1/1 empty page."""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from ccbot.handlers.notifications import (
    CardState,
    Event,
    _ensure_seeded,
    _seed_events_from_jsonl,
)
from ccbot.handlers.card_types import PendingPrompt


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

    async def test_codex_seed_uses_rollout_transcript_path(
        self, tmp_path: Path
    ) -> None:
        from ccbot.session import Session, session_manager

        rollout = tmp_path / "rollout.jsonl"
        _write_jsonl(
            rollout,
            [
                {
                    "timestamp": "2026-07-31T10:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "codex final",
                        "phase": "final_answer",
                    },
                }
            ],
        )
        ws = session_manager.get_window_state("@seed-codex")
        ws.session_id = "codex-session"
        ws.cwd = str(tmp_path)
        ws.backend = "codex"
        ws.transcript_path = str(rollout)
        sess = Session(
            id="x",
            name="y",
            backend="codex",
            window_id="@seed-codex",
        )

        events = await _seed_events_from_jsonl(sess)

        assert any(
            ev.type == "final_text" and ev.text == "codex final" for ev in events
        )


@pytest.mark.asyncio
class TestEnsureSeededIdempotent:
    async def test_remote_session_seeds_from_worker_without_local_window_state(
        self, monkeypatch
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import ccbot.handlers.card_seed as card_seed
        from ccbot.handlers.card_pagination import paginate_events_for_card
        from ccbot.session import Session

        runtime = SimpleNamespace(
            seed_session_history=AsyncMock(
                return_value={
                    "ok": True,
                    "version": "100:200",
                    "entries": [
                        {
                            "role": "user",
                            "text": "earlier question",
                            "content_type": "text",
                            "timestamp": "2026-09-21T10:00:00Z",
                        },
                        {
                            "role": "assistant",
                            "text": "earlier answer",
                            "content_type": "text",
                            "stop_reason": "end_turn",
                            "timestamp": "2026-09-21T10:00:01Z",
                        },
                    ],
                }
            )
        )
        monkeypatch.setattr(
            card_seed, "get_node_runtime", lambda _node: runtime, raising=False
        )
        monkeypatch.setattr(
            card_seed.session_manager,
            "get_user_settings",
            lambda _uid: {"card_history": 50},
        )
        state = CardState()
        sess = Session(
            id="remote",
            name="Remote",
            node_id="worker-a",
            window_id="",
            worker_session_id="routing-1",
            workdir="/worker/project",
            backend="codex",
        )

        await _ensure_seeded(42, sess, state)

        runtime.seed_session_history.assert_awaited_once_with(
            "worker-a", "routing-1", 50, known_version=""
        )
        assert [event.type for event in state.events] == ["user_msg", "final_text"]
        assert [event.text for event in state.events] == [
            "earlier question",
            "earlier answer",
        ]
        assert state.seed_attempted is True
        assert len(paginate_events_for_card(state, None)) >= 2

    async def test_remote_media_seed_consumes_caption_receipt(
        self, monkeypatch
    ) -> None:
        """Worker history uses the same media-aware turn reconciliation."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import ccbot.handlers.card_seed as card_seed
        from ccbot.session import Session

        caption = "Проверь изображение"
        runtime = SimpleNamespace(
            seed_session_history=AsyncMock(
                return_value={
                    "ok": True,
                    "version": "1:2",
                    "entries": [
                        {
                            "role": "user",
                            "text": f"{caption}\n\n.ccbot-inbox/worker-image.jpg",
                            "content_type": "text",
                            "timestamp": "2026-09-23T10:00:00Z",
                        }
                    ],
                }
            )
        )
        monkeypatch.setattr(card_seed, "get_node_runtime", lambda _node: runtime)
        state = CardState(
            pending_prompts=[
                PendingPrompt(
                    request_id="91",
                    text=caption,
                    inbox_attachment=True,
                    created_at=1.0,
                )
            ],
            pending_request_sequences=[(91, 7)],
        )
        sess = Session(
            id="remote-media",
            name="Remote media",
            node_id="worker-a",
            worker_session_id="routing-1",
            workdir="/worker/project",
            backend="codex",
        )

        await _ensure_seeded(42, sess, state)

        assert state.pending_prompts == []
        assert state.pending_request_sequences == []
        assert state.active_turn_sequence == 7

    async def test_seed_reconciles_pending_prompt_at_its_turn_position(
        self, monkeypatch
    ) -> None:
        """A seeded user echo replaces the live tail instead of duplicating it."""
        import ccbot.handlers.notifications as notif
        from ccbot.session import Session

        prompt = "Проверь корректные данные по Турции?"
        seeded_prompt = Event(
            type="user_msg",
            text=prompt,
            body=prompt,
            started_at=2.0,
            is_page_break=True,
        )
        seeded_work = Event(type="text", text="Проверяю данные", started_at=3.0)

        async def _seed(_sess, max_turns=0):
            del max_turns
            return [
                Event(
                    type="user_msg",
                    text="служебный контекст",
                    started_at=1.0,
                    is_page_break=True,
                ),
                seeded_prompt,
                seeded_work,
            ]

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _seed)
        state = CardState(
            pending_prompts=[
                PendingPrompt(
                    request_id="77",
                    text=prompt,
                    preprocessed=True,
                    created_at=1.5,
                )
            ]
        )
        sess = Session(id="turkey", name="turkey data", window_id="@120")

        await _ensure_seeded(1, sess, state)

        assert state.pending_prompts == []
        assert state.events == [state.events[0], seeded_prompt, seeded_work]
        assert seeded_prompt.user_icon == "👤💻"
        from ccbot.handlers.card_layout import _render_card

        rendered = _render_card(sess, state, user_id=1)
        assert rendered.count(prompt) == 1
        assert rendered.index(prompt) < rendered.index("Проверяю данные")

    async def test_seed_reconciles_media_caption_with_inbox_path(
        self, monkeypatch
    ) -> None:
        """A media path appended by ccbot still belongs to the caption turn."""
        import ccbot.handlers.notifications as notif
        from ccbot.session import Session

        caption = "Что это за новый режим в кодексе?"
        delivered = f"{caption}\n\n.ccbot-inbox/1790163336-image.jpg"
        seeded_prompt = Event(
            type="user_msg",
            text=delivered,
            body=delivered,
            started_at=2.0,
            is_page_break=True,
        )

        async def _seed(_sess, max_turns=0):
            del max_turns
            return [seeded_prompt]

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _seed)
        state = CardState(
            pending_prompts=[
                PendingPrompt(
                    request_id="4396",
                    text=caption,
                    inbox_attachment=True,
                    created_at=1.0,
                )
            ],
            pending_request_sequences=[(4396, 1)],
        )
        sess = Session(id="media", name="media", window_id="@55")

        await _ensure_seeded(1, sess, state)

        assert state.pending_prompts == []
        assert state.pending_request_sequences == []
        assert state.active_turn_sequence == 1
        assert state.events == [seeded_prompt]

    async def test_seed_reconciles_captionless_media_path(self, monkeypatch) -> None:
        """An image-only prompt consumes its receipt instead of shifting FIFO."""
        import ccbot.handlers.notifications as notif
        from ccbot.session import Session

        delivered = ".ccbot-inbox/1790163336-image.jpg"
        seeded_prompt = Event(
            type="user_msg",
            text=delivered,
            body=delivered,
            started_at=2.0,
            is_page_break=True,
        )

        async def _seed(_sess, max_turns=0):
            del max_turns
            return [seeded_prompt]

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _seed)
        state = CardState(
            pending_prompts=[
                PendingPrompt(
                    request_id="4396",
                    text="",
                    inbox_attachment=True,
                    created_at=1.0,
                )
            ],
            pending_request_sequences=[(4396, 1)],
        )
        sess = Session(id="media", name="media", window_id="@55")

        await _ensure_seeded(1, sess, state)

        assert state.pending_prompts == []
        assert state.pending_request_sequences == []
        assert state.active_turn_sequence == 1

    async def test_seed_preserves_user_authored_inbox_path_as_prompt(
        self, monkeypatch
    ) -> None:
        """A manually typed inbox path remains an ordinary exact prompt."""
        import ccbot.handlers.notifications as notif
        from ccbot.session import Session

        prompt = ".ccbot-inbox/report.pdf"
        seeded_prompt = Event(
            type="user_msg",
            text=prompt,
            body=prompt,
            started_at=2.0,
            is_page_break=True,
        )

        async def _seed(_sess, max_turns=0):
            del max_turns
            return [seeded_prompt]

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _seed)
        state = CardState(
            pending_prompts=[
                PendingPrompt(request_id="100", text=prompt, created_at=1.0)
            ],
            pending_request_sequences=[(100, 1)],
        )
        sess = Session(id="text-path", name="text path", window_id="@57")

        await _ensure_seeded(1, sess, state)

        assert state.pending_prompts == []
        assert state.pending_request_sequences == []
        assert state.active_turn_sequence == 1

    async def test_seed_reconciles_concatenated_pending_prompt_batch(
        self, monkeypatch
    ) -> None:
        import ccbot.handlers.notifications as notif
        from ccbot.session import Session

        combined = "Первый запрос.Второй запрос"
        seeded_prompt = Event(
            type="user_msg",
            text=combined,
            body=combined,
            started_at=3.0,
            is_page_break=True,
        )

        async def _seed(_sess, max_turns=0):
            del max_turns
            return [seeded_prompt]

        monkeypatch.setattr(notif, "_seed_events_from_jsonl", _seed)
        state = CardState(
            pending_prompts=[
                PendingPrompt(request_id="81", text="Первый запрос.", created_at=1.0),
                PendingPrompt(
                    request_id="82",
                    text="Второй запрос",
                    preprocessed=True,
                    created_at=2.0,
                ),
            ],
            pending_request_sequences=[(81, 41), (82, 42)],
        )
        sess = Session(id="international", name="international marts", window_id="@99")

        await _ensure_seeded(1, sess, state)

        assert state.pending_prompts == []
        assert state.pending_request_sequences == []
        assert state.active_turn_sequence == 42
        assert seeded_prompt.user_icon == "👤💻"
        from ccbot.handlers.card_layout import _render_card

        rendered = _render_card(sess, state, user_id=1)
        assert rendered.count("Первый запрос.") == 1
        assert rendered.count("Второй запрос") == 1

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
