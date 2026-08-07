"""End-to-end state contract for a silent unfinished active turn."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.card_types import CardState, Event, TurnPhase
from ccbot.session_models import Session


@pytest.mark.asyncio
async def test_silent_turn_preserves_event_and_live_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = 42
    sess = Session(
        id="s1",
        name="active",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="uuid-s1",
    )
    event = Event(type="tool_use", text="Bash", started_at=time.time() - 400)
    state = CardState(msg_id=77, events=[event], last_event_ts=time.time() - 400)
    notifications._cards[(user_id, sess.id)] = state
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: sess
    )
    monkeypatch.setattr(notifications, "_render_card", lambda *_a, **_k: "live")
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, "_edit_card", edit)

    try:
        assert await notifications.maybe_finalize_stalled(
            SimpleNamespace(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )
        assert state.events == [event]
        assert state.turn_phase is TurnPhase.RUNNING
        edit.assert_awaited_once()
    finally:
        notifications._cards.clear()
