"""Interactive card refreshes bypass the normal live-update debounce."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_surface
from ccbot.handlers.card_model import CardState, Event


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


@pytest.mark.asyncio
async def test_keyboard_refresh_paints_when_card_text_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=7, last_rendered="same text")
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "_render_card": lambda *_a, **_k: "same text",
            "_edit_card": edit,
        }[name],
    )

    try:
        assert await card_surface.refresh_panel(
            SimpleNamespace(), 42, immediate=True, refresh_keyboard=True
        )
        edit.assert_awaited_once()
        assert edit.await_args.kwargs["refresh_pane"] is False
    finally:
        card_surface._cards.pop((42, "s1"), None)


@pytest.mark.asyncio
async def test_status_keyboard_refresh_does_not_edit_card_text(monkeypatch) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=7, last_rendered="unchanged card text")
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    keyboard = SimpleNamespace(inline_keyboard=[[SimpleNamespace(text="✅ target")]])
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "build_footer_keyboard": lambda *_args, **_kwargs: keyboard,
        }[name],
    )
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(return_value=True),
        edit_message_text=AsyncMock(),
        edit_message_media=AsyncMock(),
    )

    try:
        assert await card_surface.refresh_session_keyboard(bot, 42)
        bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=42,
            message_id=7,
            reply_markup=keyboard,
        )
        bot.edit_message_text.assert_not_awaited()
        bot.edit_message_media.assert_not_awaited()
        assert state.last_rendered == "unchanged card text"
    finally:
        card_surface._cards.pop((42, "s1"), None)


@pytest.mark.asyncio
async def test_automatic_refresh_joins_the_live_lag_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(msg_id=7, last_rendered="old", last_edit_ts=100.0)
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    monkeypatch.setattr(
        card_surface.session_manager,
        "get_user_settings",
        lambda _uid: {"live_lag": 2},
    )
    monkeypatch.setattr(card_surface.time, "monotonic", lambda: 101.0)
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {"_render_card": lambda *_a, **_k: "new", "_edit_card": edit}[
            name
        ],
    )

    try:
        assert await card_surface.refresh_panel(SimpleNamespace(), 42)
        edit.assert_not_awaited()
        assert state.pending_edit is not None
    finally:
        if state.pending_edit is not None:
            state.pending_edit.cancel()
            await asyncio.gather(state.pending_edit, return_exceptions=True)
        card_surface._cards.pop((42, "s1"), None)


@pytest.mark.asyncio
async def test_card_timer_respects_configured_live_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="s1")
    state = CardState(
        msg_id=7,
        last_rendered="old",
        last_edit_ts=100.0,
        events=[Event(type="tool_use", text="call", started_at=1.0)],
    )
    card_surface._cards[(42, "s1")] = state
    monkeypatch.setattr(
        card_surface.session_manager, "get_session", lambda _sid: session
    )
    monkeypatch.setattr(
        card_surface.session_manager, "get_active_session", lambda _uid: session
    )
    monkeypatch.setattr(
        card_surface.session_manager,
        "get_user_settings",
        lambda _uid: {"live_lag": 4},
    )
    monkeypatch.setattr(card_surface.time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(
        card_surface.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
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
        await card_surface.card_timer_loop(SimpleNamespace())
        edit.assert_not_awaited()
    finally:
        card_surface._cards.pop((42, "s1"), None)
