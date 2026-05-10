"""Tests for ccbot.bot._common helpers (pure logic only)."""

from ccbot.bot._common import shorten_workdir


class TestShortenWorkdir:
    def test_empty_string_returns_question_mark(self) -> None:
        assert shorten_workdir("") == "?"

    def test_short_path_unchanged(self) -> None:
        assert shorten_workdir("/tmp") == "/tmp"

    def test_collapses_home_to_tilde(self) -> None:
        import os

        home = os.path.expanduser("~")
        assert shorten_workdir(f"{home}/proj").startswith("~/")
