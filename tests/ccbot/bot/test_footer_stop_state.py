"""Stop is an authoritative live-card state transition."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import footer
from ccbot.handlers.callback_data import CB_FT_STOP
from ccbot.handlers.card_model import CardState, Event, TurnPhase, _card_is_busy
from ccbot.handlers.card_surface import surface_card_after_message


@pytest.mark.asyncio
async def test_stop_immediately_makes_session_closable_and_survives_stale_working_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = SimpleNamespace(id="s1", window_id="@1")
    state = CardState(
        msg_id=9,
        events=[Event(type="tool_use", text="call", started_at=1.0)],
        last_event_ts=time.time(),
        turn_phase=TurnPhase.RUNNING,
    )
    query = SimpleNamespace(data=CB_FT_STOP, answer=AsyncMock())
    context = SimpleNamespace(bot=object())
    user = SimpleNamespace(id=42)
    window = SimpleNamespace(window_id="@1")
    refresh = AsyncMock(return_value=True)
    send_keys = AsyncMock(return_value=True)

    monkeypatch.setattr(footer, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(
        footer.tmux_manager, "find_window_by_id", AsyncMock(return_value=window)
    )
    monkeypatch.setattr(footer.tmux_manager, "send_keys", send_keys)
    monkeypatch.setattr(footer.session_manager, "get_active_session", lambda _uid: sess)
    monkeypatch.setattr(footer, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(footer, "refresh_panel", refresh)

    assert _card_is_busy(state) is True
    assert await footer.handle(query, context, user) is True
    send_keys.assert_awaited_once_with("@1", "\x1b", enter=False)
    assert _card_is_busy(state) is False
    refresh.assert_awaited_once_with(
        context.bot, 42, immediate=True, refresh_keyboard=True
    )

    from ccbot.handlers import status_polling

    monkeypatch.setattr(status_polling, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(status_polling, "parse_status_line", lambda _p: "Working (18s)")
    monkeypatch.setattr(status_polling, "is_card_in_menu_view", lambda *_a: False)
    monkeypatch.setattr(status_polling, "is_card_busy", lambda *_a: False)
    monkeypatch.setattr(status_polling, "is_card_finalized", lambda *_a: False)
    monkeypatch.setattr(status_polling, "_pane_status_is_changing", lambda *_a: True)
    monkeypatch.setattr(status_polling, "maybe_finalize_stalled", AsyncMock())
    monkeypatch.setattr(status_polling, "refresh_panel", AsyncMock(return_value=True))
    fire_typing = AsyncMock()
    monkeypatch.setattr(status_polling, "fire_typing", fire_typing)
    monkeypatch.setattr(status_polling, "is_interactive_ui", lambda _p: False)
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u: None)

    await status_polling._drive_typing_indicator(
        object(), 42, "@1", "Working (18s - esc to interrupt)", sess, False
    )

    assert _card_is_busy(state) is False
    fire_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_next_user_message_resumes_stopped_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = SimpleNamespace(id="s1")
    state = CardState(msg_id=9, turn_phase=TurnPhase.IDLE)
    state.user_stopped = True
    bot = AsyncMock()

    async def send_card(_bot, _user_id, _sess, target, **_kwargs):
        target.msg_id = 11

    monkeypatch.setattr("ccbot.handlers.card_surface.get_card_state", lambda *_a: state)
    monkeypatch.setattr(
        "ccbot.handlers.card_surface._legacy",
        lambda name: {
            "_ensure_seeded": AsyncMock(),
            "is_active_for_user": lambda *_a: True,
            "_render_card": lambda *_a, **_k: "card",
            "_send_card": send_card,
        }[name],
    )

    assert await surface_card_after_message(bot, 42, sess, 10) is True
    assert state.user_stopped is False
    assert state.turn_phase is TurnPhase.RUNNING
