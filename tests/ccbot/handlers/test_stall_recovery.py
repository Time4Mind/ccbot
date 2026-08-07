"""A real event or final answer exits silent-turn observation cleanly."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_updates
from ccbot.handlers.card_types import CardState, Event, TurnPhase


@pytest.mark.asyncio
async def test_final_clears_stall_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    state = CardState(
        msg_id=9,
        events=[Event(type="thinking", text="work", started_at=1.0)],
        stall_watch_active=True,
        last_stall_pane_refresh_ts=3.0,
    )
    sess = SimpleNamespace(id="s1", window_id="")
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_a: False)
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: {
            "_ensure_seeded": AsyncMock(),
            "_render_card": lambda *_a, **_k: "final",
            "build_footer_keyboard": lambda *_a, **_k: None,
            "_edit_card": AsyncMock(return_value=True),
        }[name],
    )

    await card_updates.finalize_task(SimpleNamespace(), 42, sess, "done")

    assert state.turn_phase is TurnPhase.IDLE
    assert state.stall_watch_active is False
    assert state.last_stall_pane_refresh_ts == 0.0
