"""Settings text preserves intentional line breaks in Rich Markdown."""

from __future__ import annotations

import pytest

from ccbot.handlers.menu import render_settings_group_text, render_settings_text
from ccbot.handlers.menu_settings_data import _GROUP_TEXT_KEYS
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
def test_settings_body_uses_hard_breaks_without_losing_paragraphs(
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
    expected = TRANSLATIONS[language]["settings.body"].format(
        agent="Claude", language=language, live_lag=4, voice="auto"
    )

    assert rendered.replace("  \n", "\n") == expected
    assert "\n\n" in rendered
    _assert_hard_single_breaks(rendered)
    _assert_hard_single_breaks(to_rich_markdown(rendered))


@pytest.mark.parametrize("language", ["en", "ru", "zh"])
def test_every_settings_group_keeps_locale_text_and_hard_breaks(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {"language": language},
    )

    for screen, key in _GROUP_TEXT_KEYS.items():
        rendered = render_settings_group_text(42, screen)  # type: ignore[arg-type]
        expected = TRANSLATIONS[language].get(key) or TRANSLATIONS["en"][key]
        assert rendered.replace("  \n", "\n") == expected
        _assert_hard_single_breaks(rendered)
