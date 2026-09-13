"""User-visible layout of the live-card Options disclosure."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ccbot.bot.callbacks import footer
from ccbot.handlers import menu
from ccbot.handlers.callback_data import (
    CB_FT_CLEAR,
    CB_FT_MORE,
    CB_FT_OPTIONS,
    CB_FT_STOP,
    CB_FT_TERM,
    CB_MM_SHOT,
    CB_SW_NEW,
    CB_SW_NOOP,
)
from ccbot.session import session_manager


def _patch_active(monkeypatch: pytest.MonkeyPatch, *, terminal: bool) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)
    monkeypatch.setattr(menu, "_has_pending_kb_action", lambda _uid: False)
    monkeypatch.setattr(menu, "can_offer_terminal", lambda _uid: terminal)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru"},
    )
    monkeypatch.setattr(
        menu,
        "build_switcher_keyboard",
        lambda *_args, **_kwargs: InlineKeyboardMarkup(
            [[InlineKeyboardButton("session", callback_data=CB_SW_NOOP)]]
        ),
    )


def test_options_replaces_shot_terminal_and_clear_on_live_card(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=True)

    keyboard = menu.build_footer_keyboard(42, screen="main", is_busy=True)
    assert keyboard is not None
    controls_row = next(
        row
        for row in keyboard.inline_keyboard
        if any(button.callback_data == CB_FT_STOP for button in row)
    )
    assert [button.callback_data for button in controls_row] == [
        CB_FT_STOP,
        CB_FT_OPTIONS,
    ]
    assert controls_row[1].text == "⋯ Опции"

    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert CB_FT_CLEAR not in callbacks
    assert CB_MM_SHOT not in callbacks
    assert CB_FT_TERM not in callbacks


def test_options_discloses_actions_directly_above_sessions(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=True)
    menu.toggle_footer_options(42)
    try:
        keyboard = menu.build_footer_keyboard(42, screen="main")
        assert keyboard is not None
        rows = [
            [button.callback_data for button in row] for row in keyboard.inline_keyboard
        ]
        assert [CB_MM_SHOT, CB_FT_TERM] in rows
        assert rows.index([CB_MM_SHOT, CB_FT_TERM]) + 1 == rows.index([CB_SW_NOOP])
    finally:
        menu.close_footer_options(42)


def test_disclosed_actions_hide_unavailable_terminal(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=False)
    menu.toggle_footer_options(42)
    try:
        keyboard = menu.build_footer_keyboard(42, screen="main")
        assert keyboard is not None
        callbacks = {
            button.callback_data for row in keyboard.inline_keyboard for button in row
        }
        assert CB_MM_SHOT in callbacks
        assert CB_FT_TERM not in callbacks
    finally:
        menu.close_footer_options(42)


def test_menu_stays_in_original_bottom_row(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: False)

    keyboard = menu.build_footer_keyboard(42, screen="main")
    assert keyboard is not None
    assert [button.callback_data for button in keyboard.inline_keyboard[-1]] == [
        CB_SW_NEW,
        CB_FT_MORE,
    ]
    callbacks = {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert CB_FT_OPTIONS not in callbacks


@pytest.mark.asyncio
async def test_options_button_toggles_and_refreshes_live_card(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=True)
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(footer, "refresh_panel", refresh)
    query = SimpleNamespace(data=CB_FT_OPTIONS, answer=AsyncMock())
    context = SimpleNamespace(bot=object())
    user = SimpleNamespace(id=42)

    try:
        assert await footer.handle(query, context, user) is True
        keyboard = menu.build_footer_keyboard(42, screen="main")
        assert keyboard is not None
        callbacks = {
            button.callback_data for row in keyboard.inline_keyboard for button in row
        }
        assert CB_MM_SHOT in callbacks
        query.answer.assert_awaited_once()
        refresh.assert_awaited_once_with(context.bot, 42, immediate=True)

        assert await footer.handle(query, context, user) is True
        collapsed = menu.build_footer_keyboard(42, screen="main")
        assert collapsed is not None
        assert all(
            button.callback_data != CB_MM_SHOT
            for row in collapsed.inline_keyboard
            for button in row
        )
        assert refresh.await_count == 2
    finally:
        menu.close_footer_options(42)
