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
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_OPTIONS,
    CB_FT_STOP,
    CB_FT_TERM,
    CB_MM_ARCHIVE,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_STATUS,
    CB_SW_NEW,
    CB_SW_NOOP,
)
from ccbot.session import session_manager

SCREENSHOT_CB = "ft:shot"


def test_main_menu_has_four_buttons_and_no_status_button(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)

    keyboard = menu.build_footer_keyboard(42, screen="more")

    assert keyboard is not None
    callbacks = [
        [button.callback_data for button in row] for row in keyboard.inline_keyboard
    ]
    assert callbacks == [
        [CB_MM_LIST, CB_MM_ARCHIVE],
        [CB_MM_NEW, CB_MM_SETTINGS],
    ]
    assert CB_MM_STATUS not in {value for row in callbacks for value in row}


def test_page_size_choices_are_line_limits_30_50_70_100(monkeypatch) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru", "card_page_lines": 50},
    )

    keyboard = menu.build_footer_keyboard(42, screen="settings_pagesize")

    assert keyboard is not None
    assert [button.text for button in keyboard.inline_keyboard[0]] == [
        "30",
        "• 50",
        "70",
        "100",
    ]


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
    assert "mm:shot" not in callbacks
    assert CB_FT_TERM not in callbacks


def test_idle_session_close_button_uses_cross_without_changing_callback(
    monkeypatch,
) -> None:
    _patch_active(monkeypatch, terminal=True)

    keyboard = menu.build_footer_keyboard(42, screen="main", is_busy=False)
    assert keyboard is not None
    close = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data == CB_FT_KILL
    )

    assert close.text == "✕ Закрыть"


def test_options_discloses_actions_directly_above_sessions(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=True)
    menu.toggle_footer_options(42)
    try:
        keyboard = menu.build_footer_keyboard(42, screen="main")
        assert keyboard is not None
        rows = [
            [button.callback_data for button in row] for row in keyboard.inline_keyboard
        ]
        assert [SCREENSHOT_CB, CB_FT_TERM] in rows
        assert rows.index([SCREENSHOT_CB, CB_FT_TERM]) + 1 == rows.index([CB_SW_NOOP])
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
        assert SCREENSHOT_CB in callbacks
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
        assert SCREENSHOT_CB in callbacks
        query.answer.assert_awaited_once()
        refresh.assert_awaited_once_with(
            context.bot, 42, immediate=True, refresh_keyboard=True
        )

        assert await footer.handle(query, context, user) is True
        collapsed = menu.build_footer_keyboard(42, screen="main")
        assert collapsed is not None
        assert all(
            button.callback_data != SCREENSHOT_CB
            for row in collapsed.inline_keyboard
            for button in row
        )
        assert refresh.await_count == 2
    finally:
        menu.close_footer_options(42)


@pytest.mark.asyncio
async def test_screenshot_action_toggles_global_state_on_current_card(
    monkeypatch,
) -> None:
    settings = {"card_inline_screenshots": False}
    updates: list[tuple[int, str, bool]] = []
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(
        session_manager, "get_user_settings", lambda _uid: dict(settings)
    )

    def update(user_id: int, key: str, value: bool) -> None:
        updates.append((user_id, key, value))
        settings[key] = value

    monkeypatch.setattr(session_manager, "update_user_setting", update)
    monkeypatch.setattr(footer, "refresh_panel", refresh)
    query = SimpleNamespace(data=SCREENSHOT_CB, answer=AsyncMock())
    context = SimpleNamespace(bot=object())
    user = SimpleNamespace(id=42)

    assert await footer.handle(query, context, user) is True

    assert updates == [(42, "card_inline_screenshots", True)]
    refresh.assert_awaited_once_with(
        context.bot, 42, immediate=True, refresh_keyboard=True
    )


def test_options_respect_configured_button_visibility(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=True)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "language": "ru",
            "option_button_screenshot": False,
            "option_button_terminal": True,
        },
    )
    menu.toggle_footer_options(42)
    try:
        keyboard = menu.build_footer_keyboard(42, screen="main")
        assert keyboard is not None
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard for button in row
        ]
        assert SCREENSHOT_CB not in callbacks
        assert CB_FT_TERM in callbacks
    finally:
        menu.close_footer_options(42)


def test_options_control_is_hidden_when_no_action_is_available(monkeypatch) -> None:
    _patch_active(monkeypatch, terminal=False)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "language": "ru",
            "option_button_screenshot": False,
            "option_button_terminal": False,
        },
    )

    keyboard = menu.build_footer_keyboard(42, screen="main")

    assert keyboard is not None
    callbacks = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert CB_FT_OPTIONS not in callbacks
