from __future__ import annotations

import json
from unittest.mock import ANY, AsyncMock

import pytest

from ccbot.bot import session_events
from ccbot.handlers import bg_status
from ccbot.handlers.callback_data import CB_SW_USE
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage
from ccbot.transcript_parser import TranscriptParser


@pytest.fixture(autouse=True)
def _isolated_statuses():
    saved = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    bg_status._bg.clear()
    yield
    bg_status._bg.clear()
    bg_status._bg.update(saved)


def _session() -> Session:
    return Session(
        id="sess1",
        name="status-test",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="claude-1",
    )


def _patch_route(
    monkeypatch: pytest.MonkeyPatch, sess: Session, *, active: bool = True
) -> None:
    monkeypatch.setattr(
        session_events.session_manager,
        "all_user_sessions_with_claude_id",
        lambda _sid: [(42, sess)],
    )
    monkeypatch.setattr(
        session_events.session_manager, "touch_session", lambda _sid: None
    )
    monkeypatch.setattr(session_events, "is_active_for_user", lambda *_args: active)
    monkeypatch.setattr(session_events, "fire_typing", AsyncMock())
    monkeypatch.setattr(session_events, "update_session_card", AsyncMock())
    monkeypatch.setattr(session_events, "finalize_task", AsyncMock())
    monkeypatch.setattr(
        session_events, "context_pct_for_session", AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_active_session_turn_transitions_working_to_finished(monkeypatch) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess)

    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="progress",
            is_complete=False,
            role="assistant",
        ),
        AsyncMock(),
    )
    assert bg_status.status_emoji(42, sess.id) == "🔶"

    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="done",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
        ),
        AsyncMock(),
    )
    assert bg_status.status_emoji(42, sess.id) == "✅"
    # Completion was shown while active, so entering it once more is the
    # second presentation and acknowledges the result.
    assert bg_status.record_finished_view(42, sess.id) is True
    assert bg_status.status_emoji(42, sess.id) == ""


@pytest.mark.asyncio
async def test_successful_retry_clears_error_but_progress_does_not(monkeypatch) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess)
    bg_status.update_status(42, sess.id, "error")

    await session_events.handle_new_message(
        NewMessage(session_id="claude-1", text="retrying", is_complete=True),
        AsyncMock(),
    )
    assert bg_status.status_emoji(42, sess.id) == "❗"

    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="recovered",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
        ),
        AsyncMock(),
    )
    assert bg_status.status_emoji(42, sess.id) == "✅"


@pytest.mark.asyncio
async def test_terminal_api_error_sets_sticky_attention_marker(monkeypatch) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess)

    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="backend failed",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
            api_error="backend_error",
        ),
        AsyncMock(),
    )

    assert bg_status._bg[42][sess.id].status == "error"
    assert bg_status.status_emoji(42, sess.id) == "❗"


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("failed", [True, False])
async def test_codex_task_completion_drives_session_button(
    monkeypatch, active, failed
) -> None:
    from ccbot.handlers.switcher import build_switcher_keyboard

    sess = _session()
    sess.backend = "codex"
    _patch_route(monkeypatch, sess, active=active)
    manager = session_events.session_manager
    monkeypatch.setattr(
        manager, "get_active_session", lambda _uid: sess if active else None
    )
    monkeypatch.setattr(manager, "list_user_sessions", lambda *_a, **_kw: [sess])
    monkeypatch.setattr(
        manager,
        "get_user_settings",
        lambda _uid: {"bg_notify_error": False, "bg_notify_finished": False},
    )
    monkeypatch.setattr(session_events, "refresh_session_keyboard", AsyncMock())
    rows = [
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "progress" if failed else "done",
                "phase": "commentary" if failed else "final_answer",
            },
        },
        {
            "timestamp": "2026-09-30T13:12:32.170Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {
                    "message": "Selected model is at capacity. Please try a different model.",
                    "codex_error_info": "server_overloaded",
                }
                if failed
                else None,
            },
        },
    ]
    parsed, _pending = TranscriptParser.parse_entries(rows)
    for entry in parsed:
        await session_events.handle_new_message(
            NewMessage(
                session_id="claude-1",
                text=entry.text,
                is_complete=True,
                role=entry.role,
                content_type=entry.content_type,
                stop_reason=entry.stop_reason,
                api_error=entry.api_error,
            ),
            AsyncMock(),
        )
    keyboard = build_switcher_keyboard(42)
    label = next(
        b.text
        for row in keyboard.inline_keyboard
        for b in row
        if b.callback_data == CB_SW_USE + sess.id
    )
    assert ("❗" if failed else "✅") in label
    assert len(parsed) == (2 if failed else 1)
    if failed:
        assert (
            parsed[-1].text
            == "Selected model is at capacity. Please try a different model."
        )


@pytest.mark.asyncio
async def test_startup_restores_codex_task_failure_from_transcript(
    monkeypatch, tmp_path
) -> None:
    from ccbot.bot._startup_status import seed_lifecycle_statuses
    from ccbot import session_claude_io

    sess = _session()
    sess.backend = "codex"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "codex_error_info": "server_overloaded",
                    },
                },
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        session_claude_io, "build_session_file_path", lambda *_a: transcript
    )
    monkeypatch.setattr(session_events.config, "allowed_users", {42})
    monkeypatch.setattr(session_events.session_manager, "sessions", {sess.id: sess})
    monkeypatch.setattr(session_events.session_manager, "save_state", lambda: None)
    bg_status.update_status(42, sess.id, "working")

    await seed_lifecycle_statuses()

    assert bg_status.status_emoji(42, sess.id) == "❗"


@pytest.mark.asyncio
async def test_background_terminal_api_error_pushes_attention_once(monkeypatch) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess, active=False)
    push_event = AsyncMock()
    refresh_panel = AsyncMock()
    refresh_keyboard = AsyncMock(return_value=True)
    monkeypatch.setattr(session_events, "refresh_panel", refresh_panel)
    monkeypatch.setattr(
        session_events,
        "refresh_session_keyboard",
        refresh_keyboard,
        raising=False,
    )
    monkeypatch.setattr(
        session_events.session_manager,
        "get_user_settings",
        lambda _user_id: {"bg_notify_error": True},
    )
    monkeypatch.setattr("ccbot.handlers.notifications.push_event", push_event)

    message = NewMessage(
        session_id="claude-1",
        text="backend failed",
        is_complete=True,
        role="assistant",
        stop_reason="end_turn",
        api_error="backend_error",
    )
    await session_events.handle_new_message(message, AsyncMock())
    await session_events.handle_new_message(message, AsyncMock())

    push_event.assert_awaited_once_with(
        ANY,
        42,
        sess,
        status="error",
        text="error",
    )
    refresh_keyboard.assert_awaited_once()
    refresh_panel.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_completion_refreshes_active_keyboard_only(
    monkeypatch,
) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess, active=False)
    refresh_keyboard = AsyncMock(return_value=True)
    refresh_panel = AsyncMock()
    monkeypatch.setattr(session_events, "refresh_session_keyboard", refresh_keyboard)
    monkeypatch.setattr(session_events, "refresh_panel", refresh_panel)
    monkeypatch.setattr(
        session_events.session_manager,
        "get_user_settings",
        lambda _user_id: {"bg_notify_finished": False},
    )

    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="done",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
        ),
        AsyncMock(),
    )

    assert bg_status.status_emoji(42, sess.id) == "✅"
    refresh_keyboard.assert_awaited_once()
    refresh_panel.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_badge_survives_stall_poll_during_keyboard_refresh(
    monkeypatch,
) -> None:
    from ccbot.handlers import card_stall
    from ccbot.handlers.card_types import CardState, Event

    sess = _session()
    _patch_route(monkeypatch, sess, active=False)
    state = CardState(
        events=[Event(type="text", text="last progress", started_at=100.0)],
        last_event_ts=100.0,
        in_menu_view=True,
        stall_watch_active=True,
    )
    monkeypatch.setitem(card_stall._cards, (42, sess.id), state)
    monkeypatch.setattr(card_stall, "is_active_for_user", lambda *_args: False)
    monkeypatch.setattr(card_stall, "_legacy", lambda _name: AsyncMock())
    monkeypatch.setattr(
        session_events.session_manager,
        "get_user_settings",
        lambda _uid: {"bg_notify_finished": False},
    )

    async def refresh_keyboard(*_args):
        await card_stall.maybe_finalize_stalled(
            AsyncMock(),
            42,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=True,
            now=500.0,
        )

    monkeypatch.setattr(session_events, "refresh_session_keyboard", refresh_keyboard)
    bg_status.update_status(42, sess.id, "stalled")
    await session_events.handle_new_message(
        NewMessage(
            session_id="claude-1",
            text="done",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
        ),
        AsyncMock(),
    )

    assert bg_status.status_emoji(42, sess.id) == "✅"
