from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.card_model import CardState, Event
from ccbot.session_models import Session


@pytest.mark.asyncio
async def test_clear_card_keeps_empty_seed_latched_state() -> None:
    user_id = 7
    sess = Session(id="sess", name="workdir", window_id="@5", state="active")
    state = CardState(
        msg_id=99,
        events=[Event(type="user_text", text="old context", started_at=1.0)],
        context_pct=42,
        seed_attempted=False,
        in_menu_view=True,
    )
    notifications._cards[(user_id, sess.id)] = state
    try:
        with patch.object(
            notifications, "_edit_card", new=AsyncMock(return_value=True)
        ):
            await notifications.clear_card(AsyncMock(), user_id, sess)

        kept = notifications._cards[(user_id, sess.id)]
        assert kept is state
        assert kept.events == []
        assert kept.current_page_idx is None
        assert kept.context_pct == 0
        assert kept.seed_attempted is True
        assert kept.in_menu_view is False
    finally:
        notifications._cards.pop((user_id, sess.id), None)
