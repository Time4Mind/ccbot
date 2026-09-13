"""User-visible layout of the live-card and Options keyboards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import more_menu
from ccbot.handlers import menu
from ccbot.handlers.callback_data import (
    CB_FT_CLEAR,
    CB_FT_MORE,
    CB_FT_STOP,
    CB_MM_SHOT,
    CB_MM_TERM,
)
from ccbot.session import session_manager


def test_options_replaces_live_card_shot_terminal_and_clear(
    monkeypatch,
) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)
    monkeypatch.setattr(menu, "_has_pending_kb_action", lambda _uid: False)
    monkeypatch.setattr(menu, "can_offer_terminal", lambda _uid: True)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru"},
    )

    keyboard = menu.build_footer_keyboard(42, screen="main", is_busy=True)
    assert keyboard is not None
    controls_row = next(
        row
        for row in keyboard.inline_keyboard
        if any(button.callback_data == CB_FT_STOP for button in row)
    )
    assert [button.callback_data for button in controls_row] == [
        CB_FT_STOP,
        CB_FT_MORE,
    ]
    assert controls_row[1].text == "≡ Опции"

    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert CB_FT_CLEAR not in callbacks
    assert CB_MM_SHOT not in callbacks
    assert CB_MM_TERM not in callbacks
    assert (
        sum(
            button.callback_data == CB_FT_MORE
            for row in keyboard.inline_keyboard
            for button in row
        )
        == 1
    )


def test_options_screen_contains_shot_and_available_terminal(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)
    monkeypatch.setattr(menu, "can_offer_terminal", lambda _uid: True)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru"},
    )

    keyboard = menu.build_footer_keyboard(42, screen="more")
    assert keyboard is not None
    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        CB_MM_SHOT,
        CB_MM_TERM,
    ]


def test_options_screen_hides_unavailable_terminal(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)
    monkeypatch.setattr(menu, "can_offer_terminal", lambda _uid: False)

    keyboard = menu.build_footer_keyboard(42, screen="more")
    assert keyboard is not None
    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert CB_MM_SHOT in callbacks
    assert CB_MM_TERM not in callbacks


def test_options_remains_available_without_active_session(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: False)

    keyboard = menu.build_footer_keyboard(42, screen="main")
    assert keyboard is not None
    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert CB_FT_MORE in callbacks
    assert CB_MM_SHOT not in callbacks


@pytest.mark.asyncio
async def test_options_terminal_keeps_options_keyboard(monkeypatch) -> None:
    session = SimpleNamespace(window_id="@7")
    monkeypatch.setattr(
        more_menu.session_manager, "get_active_session", lambda _uid: session
    )
    open_terminal = AsyncMock()
    monkeypatch.setattr("ccbot.local_terminal.open_terminal_for_window", open_terminal)
    options_keyboard = object()
    monkeypatch.setattr(
        more_menu,
        "build_footer_keyboard",
        lambda _uid, *, screen: options_keyboard if screen == "more" else None,
    )
    query = SimpleNamespace(
        data=CB_MM_TERM,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(bot=object())
    user = SimpleNamespace(id=42)

    assert await more_menu.handle(query, context, user) is True
    open_terminal.assert_awaited_once_with("@7", user_id=42)
    query.edit_message_reply_markup.assert_awaited_once_with(
        reply_markup=options_keyboard
    )
