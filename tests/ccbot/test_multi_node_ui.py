from __future__ import annotations

from telegram import InlineKeyboardMarkup

from ccbot.handlers import menu
from ccbot.handlers.callback_data import (
    CB_FT_TRANSFER,
    CB_MM_NEW,
    CB_MM_NODES,
    CB_MM_SETTINGS,
    CB_SW_NEW,
)


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[list[str | None]]:
    return [
        [button.callback_data for button in row] for row in keyboard.inline_keyboard
    ]


def test_nodes_button_is_in_main_menu_only_with_multiple_nodes(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: True)

    keyboard = menu.build_footer_keyboard(42, screen="more")

    assert keyboard is not None
    assert CB_MM_NODES in {callback for row in _callbacks(keyboard) for callback in row}


def test_single_node_menu_keeps_settings_but_omits_nodes_and_new(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: False)

    keyboard = menu.build_footer_keyboard(42, screen="more")

    assert keyboard is not None
    callbacks = {callback for row in _callbacks(keyboard) for callback in row}
    assert CB_MM_SETTINGS in callbacks
    assert CB_MM_NODES not in callbacks
    assert CB_MM_NEW not in callbacks


def test_sessions_bottom_row_places_nodes_between_new_and_menu(monkeypatch) -> None:
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: True)
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: False)

    keyboard = menu.build_footer_keyboard(42, screen="main")

    assert keyboard is not None
    assert _callbacks(keyboard)[-1] == [CB_SW_NEW, CB_MM_NODES, menu.CB_FT_MORE]


def test_transfer_action_requires_setting_multiple_nodes_and_idle_session(
    monkeypatch,
) -> None:
    monkeypatch.setattr(menu, "_has_active_session", lambda _uid: True)
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: True)
    monkeypatch.setattr(menu, "can_offer_terminal", lambda _uid: False)
    monkeypatch.setattr(
        menu.session_manager,
        "get_user_settings",
        lambda _uid: {"language": "en", "option_button_transfer": True},
    )
    monkeypatch.setattr(menu, "_has_pending_kb_action", lambda _uid: False)

    menu.toggle_footer_options(42)
    try:
        idle = menu.build_footer_keyboard(42, screen="main", is_busy=False)
        busy = menu.build_footer_keyboard(42, screen="main", is_busy=True)

        assert idle is not None
        assert busy is not None
        assert CB_FT_TRANSFER in {
            callback for row in _callbacks(idle) for callback in row
        }
        assert CB_FT_TRANSFER not in {
            callback for row in _callbacks(busy) for callback in row
        }
    finally:
        menu.close_footer_options(42)
