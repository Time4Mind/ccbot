"""Silent unfinished turns stay observable without synthetic warnings."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import bg_status, notifications
from ccbot.handlers.card_types import CardState, Event, TurnPhase
from ccbot.session_models import Session


def _session(sid: str = "s1") -> Session:
    return Session(
        id=sid,
        name="tests",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id=f"uuid-{sid}",
    )


def _seed(user_id: int, sess: Session, *, tail: str = "thinking") -> CardState:
    now = time.time()
    state = CardState(
        msg_id=100,
        events=[Event(type=tail, text="unfinished", started_at=now - 400)],
        last_event_ts=now - 400,
    )
    notifications._cards[(user_id, sess.id)] = state
    return state


@pytest.fixture(autouse=True)
def _clear_state() -> None:
    notifications._cards.clear()
    bg_status._bg.clear()


@pytest.mark.asyncio
async def test_active_stall_keeps_running_pane_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, sess = 42, _session()
    state = _seed(user_id, sess)
    edit = AsyncMock(return_value=True)
    finalize = AsyncMock()
    push = AsyncMock()
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: sess
    )
    monkeypatch.setattr(notifications, "_render_card", lambda *_a, **_k: "card")
    monkeypatch.setattr(notifications, "_edit_card", edit)
    monkeypatch.setattr(notifications, "finalize_task", finalize)
    monkeypatch.setattr(notifications, "safe_send", push)

    assert await notifications.maybe_finalize_stalled(
        SimpleNamespace(),
        user_id,
        sess,
        pane_busy=False,
        interactive_waiting=False,
        in_menu=False,
    )

    assert state.turn_phase is TurnPhase.RUNNING
    assert state.stall_watch_active is True
    assert state.events[-1].type == "thinking"
    edit.assert_awaited_once()
    finalize.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_stall_sets_only_warning_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, sess = 42, _session("bg")
    state = _seed(user_id, sess)
    state.in_menu_view = True
    active = _session("active")
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: active
    )
    monkeypatch.setattr(
        notifications.session_manager,
        "get_session",
        lambda sid: sess if sid == sess.id else active,
    )
    monkeypatch.setattr(notifications, "refresh_panel", refresh)
    monkeypatch.setattr(notifications, "_edit_card", AsyncMock())

    assert await notifications.maybe_finalize_stalled(
        SimpleNamespace(),
        user_id,
        sess,
        pane_busy=False,
        interactive_waiting=False,
        in_menu=True,
    )

    assert bg_status._bg[user_id][sess.id].status == "stalled"
    assert "⚠️" in bg_status.render_panel(user_id, active_session_id=active.id)
    refresh.assert_awaited_once()
    notifications._edit_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_activity_clears_stalled_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, sess = 42, _session("bg")
    state = _seed(user_id, sess)
    state.stall_watch_active = True
    bg_status.update_status(user_id, sess.id, "stalled")
    active = _session("active")
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: active
    )
    monkeypatch.setattr(notifications, "refresh_panel", refresh)

    assert not await notifications.maybe_finalize_stalled(
        SimpleNamespace(),
        user_id,
        sess,
        pane_busy=True,
        interactive_waiting=False,
        in_menu=True,
    )

    assert bg_status._bg[user_id][sess.id].status == "working"
    assert state.stall_watch_active is False
    refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pane_busy", "interactive", "in_menu", "tail", "age"),
    [
        (True, False, False, "thinking", 400),
        (False, True, False, "thinking", 400),
        (False, False, True, "thinking", 400),
        (False, False, False, "final_text", 400),
        (False, False, False, "thinking", 10),
    ],
)
async def test_active_non_stall_states_do_not_start_watch(
    monkeypatch: pytest.MonkeyPatch,
    pane_busy: bool,
    interactive: bool,
    in_menu: bool,
    tail: str,
    age: float,
) -> None:
    user_id, sess = 42, _session()
    state = _seed(user_id, sess, tail=tail)
    state.in_menu_view = in_menu
    state.last_event_ts = time.time() - age
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: sess
    )
    edit = AsyncMock()
    monkeypatch.setattr(notifications, "_edit_card", edit)

    assert not await notifications.maybe_finalize_stalled(
        SimpleNamespace(),
        user_id,
        sess,
        pane_busy=pane_busy,
        interactive_waiting=interactive,
        in_menu=in_menu,
    )
    edit.assert_not_awaited()
