from __future__ import annotations

from unittest.mock import ANY, AsyncMock

import pytest

from ccbot.bot import session_events
from ccbot.handlers import bg_status
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


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
    assert bg_status.status_emoji(42, sess.id) == "⏳"

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
async def test_background_terminal_api_error_pushes_attention_once(monkeypatch) -> None:
    sess = _session()
    _patch_route(monkeypatch, sess, active=False)
    push_event = AsyncMock()
    refresh_panel = AsyncMock()
    monkeypatch.setattr(session_events, "refresh_panel", refresh_panel)
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
