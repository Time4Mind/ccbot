"""Interactive card refreshes bypass the normal live-update debounce."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_surface
from ccbot.handlers.card_model import CardState


async def _long_deferred_edit() -> None:
    await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_regular_refresh_keeps_pending_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=7, last_rendered="old")
    pending = asyncio.create_task(_long_deferred_edit())
    state.pending_edit = pending
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {"_render_card": lambda *_a, **_k: "new", "_edit_card": edit}[
            name
        ],
    )

    try:
        await card_surface.refresh_panel(SimpleNamespace(), 42)
        assert not pending.done()
        edit.assert_not_awaited()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        card_surface._cards.pop((42, "s1"), None)


@pytest.mark.asyncio
async def test_immediate_refresh_cancels_debounce_and_paints_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=7, last_rendered="old")
    pending = asyncio.create_task(_long_deferred_edit())
    state.pending_edit = pending
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {"_render_card": lambda *_a, **_k: "page 1", "_edit_card": edit}[
            name
        ],
    )

    try:
        await card_surface.refresh_panel(SimpleNamespace(), 42, immediate=True)
        assert pending.cancelled()
        assert state.pending_edit is None
        edit.assert_awaited_once()
        assert edit.await_args.kwargs["refresh_pane"] is False
        assert state.last_rendered == "page 1"
    finally:
        card_surface._cards.pop((42, "s1"), None)
