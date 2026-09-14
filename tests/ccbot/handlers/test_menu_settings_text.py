"""Settings text preserves intentional line breaks in Rich Markdown."""

from __future__ import annotations

import pytest

from ccbot.handlers.menu import (
    build_footer_keyboard,
    render_settings_group_text,
    render_settings_text,
)
from ccbot.handlers.menu_settings_data import SETTINGS_CATEGORIES, _GROUP_TEXT_KEYS
from ccbot.i18n import TRANSLATIONS
from ccbot.rich import to_rich_markdown
from ccbot.session import session_manager


def _assert_hard_single_breaks(text: str) -> None:
    for index, char in enumerate(text):
        if char != "\n":
            continue
        previous_is_newline = index > 0 and text[index - 1] == "\n"
        next_is_newline = index + 1 < len(text) and text[index + 1] == "\n"
        if not previous_is_newline and not next_is_newline:
            assert text[:index].endswith("  ")


@pytest.mark.parametrize("language", ["en", "ru", "zh"])
def test_settings_root_renders_category_table_matching_buttons(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "language": language,
        "live_lag": 4,
        "voice": "auto",
    }
    monkeypatch.setattr(session_manager, "get_user_settings", lambda _uid: settings)
    monkeypatch.setattr(session_manager, "agent_backend", "claude")

    rendered = render_settings_text(42)
    keyboard = build_footer_keyboard(42, screen="settings")

    assert keyboard is not None
    assert "|" in rendered
    assert "|---|---|" in rendered
    category_labels = [
        TRANSLATIONS[language][label_key]
        for label_key, _screen_name, _members in SETTINGS_CATEGORIES
    ]
    button_labels = [row[0].text for row in keyboard.inline_keyboard[:-1]]
    assert button_labels == category_labels
    for label in category_labels:
        assert f"| {label} |" in rendered
    assert "Agent: `Claude`" not in rendered
    assert "Агент: `Claude`" not in rendered
    assert "代理: `Claude`" not in rendered
    assert "<sub>" in to_rich_markdown(rendered)


@pytest.mark.parametrize("language", ["en", "ru", "zh"])
def test_terminal_setting_screens_keep_existing_locale_text(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": language},
    )

    category_screens = {screen for _label, screen, _members in SETTINGS_CATEGORIES}
    for screen, key in _GROUP_TEXT_KEYS.items():
        if screen in category_screens:
            continue
        rendered = render_settings_group_text(42, screen)  # type: ignore[arg-type]
        expected = TRANSLATIONS[language].get(key) or TRANSLATIONS["en"][key]
        assert rendered.replace("  \n", "\n") == expected
        _assert_hard_single_breaks(rendered)


def test_multi_setting_category_moves_values_from_buttons_to_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "language": "ru",
            "live_lag": 4,
            "card_history": 10,
            "card_page_lines": 20,
            "screenshot_capture_kib": 48,
            "screenshot_profile": "full8",
        },
    )

    rendered = render_settings_group_text(42, "settings_cat_card")
    keyboard = build_footer_keyboard(42, screen="settings_cat_card")

    assert keyboard is not None
    assert "| Настройка | Текущее значение |" in rendered
    assert "| Лаг карточки | 4s |" in rendered
    assert "| История в карточке | 10 turns |" in rendered
    assert "| Размер страницы | 20 lines |" in rendered
    assert "| Объём скрина | 48 KiB |" in rendered
    assert "| Качество скрина | 100%, 8 цветов |" in rendered
    assert [row[0].text for row in keyboard.inline_keyboard[:-1]] == [
        "Лаг карточки",
        "История в карточке",
        "Размер страницы",
        "Объём скрина",
        "Качество скрина",
    ]


@pytest.mark.parametrize(
    ("screen", "expected_button"),
    [
        ("settings_cat_voice", "Голос"),
        ("settings_cat_terminal", "Локальный терминал"),
    ],
)
def test_single_setting_category_has_no_summary_table(
    screen: str, expected_button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru", "voice": "auto", "local_terminal": "off"},
    )

    rendered = render_settings_group_text(42, screen)  # type: ignore[arg-type]
    keyboard = build_footer_keyboard(42, screen=screen)  # type: ignore[arg-type]

    assert keyboard is not None
    assert "|---|---|" not in rendered
    assert keyboard.inline_keyboard[0][0].text == expected_button


def test_voice_settings_offer_parakeet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru", "voice": "parakeet"},
    )

    keyboard = build_footer_keyboard(42, screen="settings_voice")
    assert keyboard is not None
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert any(value.endswith("parakeet") for value in callbacks)


def test_live_lag_settings_no_longer_offer_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": "ru", "live_lag": 2},
    )

    keyboard = build_footer_keyboard(42, screen="settings_lag")
    assert keyboard is not None
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("st:lag:")
    ]

    assert callbacks == ["st:lag:2", "st:lag:4", "st:lag:8"]


def test_options_category_renders_button_visibility_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "language": "ru",
            "option_button_screenshot": True,
            "option_button_terminal": False,
        },
    )

    rendered = render_settings_group_text(42, "settings_cat_options")
    keyboard = build_footer_keyboard(42, screen="settings_cat_options")

    assert "| Кнопка | Показывать |" in rendered
    assert "| 🧑‍💻 Скрин | on |" in rendered
    assert "| 🖥 Терминал | off |" in rendered
    assert keyboard is not None
    assert [row[0].text for row in keyboard.inline_keyboard[:-1]] == [
        "🧑‍💻 Скрин",
        "🖥 Терминал",
    ]
