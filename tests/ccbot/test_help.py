"""Tests for the /help inline-doc renderer."""

from ccbot.bot.commands.info import HELP_SECTIONS, render_help


class TestHelpHomeScreen:
    def test_home_has_section_buttons(self) -> None:
        text, kb = render_help(1, "home")
        assert "Help" in text or "Помощь" in text or "帮助" in text
        # All section buttons present.
        cb_data = {b.callback_data for row in kb.inline_keyboard for b in row}
        for s in HELP_SECTIONS:
            assert f"hlp:s:{s}" in cb_data

    def test_home_only_offers_menu_exit(self) -> None:
        _, kb = render_help(1, "home")
        last_row = kb.inline_keyboard[-1]
        # No "back to home" row on home itself.
        assert len(last_row) == 1


class TestHelpSectionScreen:
    def test_section_renders_body_text(self) -> None:
        text, _ = render_help(1, "sessions")
        # Body should be longer than the title row.
        assert len(text) > 80

    def test_section_offers_back_and_menu(self) -> None:
        _, kb = render_help(1, "sessions")
        last_row = kb.inline_keyboard[-1]
        callbacks = [b.callback_data for b in last_row]
        assert "hlp:home" in callbacks
        # CB_MM_BACK lives at "mm:back" — exits the help into the Menu screen.
        assert any(cb and cb.startswith("mm:") for cb in callbacks)

    def test_invalid_section_falls_back_to_home(self) -> None:
        text, _ = render_help(1, "bogus")
        text_home, _ = render_help(1, "home")
        assert text == text_home

    def test_active_section_marked_with_dot(self) -> None:
        _, kb = render_help(1, "menu")
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any(label.startswith("• ") for label in labels)
