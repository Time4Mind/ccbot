"""Tests for notifications._strip_for_card."""

import os

import pytest

from ccbot.handlers.notifications import _strip_for_card


class TestStripForCard:
    def test_drops_expandable_quote_block(self) -> None:
        src = (
            "  ⎿  Output 64 lines\n"
            "\x02EXPQUOTE_START\x02CLAUDE.md CONTRIBUTING.md\n"
            "doc/ ...\x02EXPQUOTE_END\x02"
        )
        out = _strip_for_card(src)
        assert "EXPQUOTE_START" not in out
        assert "EXPQUOTE_END" not in out
        assert "CLAUDE.md" not in out
        assert "Output 64 lines" in out

    def test_drops_quote_block_spanning_newlines(self) -> None:
        src = (
            "header\n\x02EXPQUOTE_START\x02line1\nline2\nline3\x02EXPQUOTE_END\x02 tail"
        )
        out = _strip_for_card(src)
        assert "line1" not in out
        assert "header" in out
        assert "tail" in out

    def test_collapses_home_to_tilde(self) -> None:
        home = os.path.expanduser("~")
        if home == "/":
            pytest.skip("home is /")
        out = _strip_for_card(f"ls {home}/proj/foo.py")
        assert out == "ls ~/proj/foo.py"

    def test_no_change_for_plain_text(self) -> None:
        assert _strip_for_card("just text") == "just text"

    def test_combined(self) -> None:
        home = os.path.expanduser("~")
        if home == "/":
            pytest.skip("home is /")
        src = f"\x02EXPQUOTE_START\x02noise\x02EXPQUOTE_END\x02 cd {home}/proj"
        out = _strip_for_card(src)
        assert "EXPQUOTE" not in out
        assert "~/proj" in out
