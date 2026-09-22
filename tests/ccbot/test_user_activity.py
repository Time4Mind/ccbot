from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ccbot import user_activity


@pytest.fixture(autouse=True)
def _reset_activity() -> None:
    user_activity.reset_for_test(started_at=100.0)
    yield
    user_activity.reset_for_test()


@pytest.mark.parametrize(
    ("idle_seconds", "expected_lag"),
    [
        (0, 4.0),
        (899.9, 4.0),
        (900, 6.0),
        (2099.9, 6.0),
        (2100, 10.0),
        (3899.9, 10.0),
        (3900, 20.0),
    ],
)
def test_default_live_lag_adapts_to_user_inactivity(
    idle_seconds: float, expected_lag: float
) -> None:
    user_activity.record(42, now=200.0)

    assert (
        user_activity.effective_live_lag(42, 4, now=200.0 + idle_seconds)
        == expected_lag
    )


def test_any_new_user_activity_resets_adaptive_lag() -> None:
    user_activity.record(42, now=200.0)
    assert user_activity.effective_live_lag(42, 4, now=4100.0) == 20.0

    user_activity.record(42, now=4100.0)

    assert user_activity.effective_live_lag(42, 4, now=4100.0) == 4.0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (2, (2.0, 3.0, 5.0, 10.0)),
        (4, (4.0, 6.0, 10.0, 20.0)),
        (8, (8.0, 12.0, 20.0, 40.0)),
    ],
)
def test_every_live_lag_uses_same_inactivity_formula(
    configured: int, expected: tuple[float, ...]
) -> None:
    user_activity.record(42, now=200.0)

    assert (
        tuple(
            user_activity.effective_live_lag(42, configured, now=200.0 + idle)
            for idle in (0, 15 * 60, 35 * 60, 65 * 60)
        )
        == expected
    )


def test_removed_zero_lag_is_normalized_to_two_seconds() -> None:
    user_activity.record(42, now=200.0)

    assert user_activity.effective_live_lag(42, 0, now=200.0) == 2.0
    assert user_activity.effective_live_lag(42, 0, now=4100.0) == 10.0


def test_no_user_action_uses_process_start_as_idle_anchor() -> None:
    assert user_activity.effective_live_lag(42, 4, now=1000.0) == 6.0


@pytest.mark.asyncio
async def test_message_activity_records_allowed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot import activity

    monkeypatch.setattr(activity, "is_user_allowed", lambda _uid: True)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    await activity.record_user_message_activity(update, MagicMock())

    assert (
        user_activity.effective_live_lag(42, 4, now=user_activity.last_seen(42)) == 4.0
    )


@pytest.mark.asyncio
async def test_any_message_clears_only_active_header_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from ccbot.bot import activity
    from ccbot.handlers import bg_status, notifications
    from ccbot.session_models import Session

    session = Session(id="active", name="active", window_id="@1", state="active")
    state = notifications.CardState(completion_marker_pending=True)
    monkeypatch.setattr(activity, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bg_status, "_bg", {})
    monkeypatch.setattr(
        activity.session_manager, "get_active_session", lambda _uid: session
    )
    monkeypatch.setattr(activity, "get_card_state", lambda _uid, _sess: state)
    refresh = AsyncMock()
    monkeypatch.setattr(activity, "refresh_panel", refresh)
    scheduled = []

    def create_task(coro, *, update):
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    bg_status.update_status(42, session.id, "finished")
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    context = SimpleNamespace(
        bot=object(), application=SimpleNamespace(create_task=create_task)
    )

    assert "✅" in notifications._render_card(session, state)

    await activity.record_user_message_activity(update, context)
    await asyncio.gather(*scheduled)

    assert "✅" not in notifications._render_card(session, state)
    assert bg_status.status_emoji(42, session.id) == "✅"
    refresh.assert_awaited_once_with(context.bot, 42, immediate=True)


@pytest.mark.asyncio
async def test_even_noop_button_records_user_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot import callbacks

    recorded: list[int] = []
    monkeypatch.setattr(callbacks, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(callbacks, "record_user_activity", recorded.append)
    active = SimpleNamespace(id="active")
    state = SimpleNamespace(
        msg_id=77,
        completion_marker_pending=True,
        in_menu_view=False,
    )
    monkeypatch.setattr(
        callbacks.session_manager, "get_active_session", lambda _uid: active
    )
    monkeypatch.setattr(callbacks, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(callbacks, "refresh_panel", AsyncMock())
    query = SimpleNamespace(
        data="noop",
        message=SimpleNamespace(message_id=999),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
    )

    await callbacks.callback_handler(update, MagicMock())

    assert recorded == [42]
    assert state.completion_marker_pending is False
    query.answer.assert_awaited_once()
