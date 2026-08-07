"""Live-card pagination uses the immediate, text-only edit path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import footer
from ccbot.handlers.callback_data import CB_PG_PREV
from ccbot.handlers.card_model import CardState


@pytest.mark.asyncio
async def test_pagination_answers_first_and_rolls_back_failed_paint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    query = SimpleNamespace(data=CB_PG_PREV)
    query.answer = AsyncMock(side_effect=lambda: order.append("answer"))
    context = SimpleNamespace(bot=SimpleNamespace())
    user = SimpleNamespace(id=42)
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=9, current_page_idx=1)

    monkeypatch.setattr(
        footer.session_manager, "get_active_session", lambda _uid: session
    )
    monkeypatch.setattr(footer, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(footer, "card_page_info", lambda _state, _uid: (1, 3))

    async def failed_refresh(_bot: object, _uid: int, **kwargs: object) -> bool:
        order.append("refresh")
        assert kwargs == {"immediate": True}
        return False

    monkeypatch.setattr(footer, "refresh_panel", failed_refresh)

    assert await footer.handle(query, context, user)
    assert order == ["answer", "refresh"]
    assert state.current_page_idx == 1
