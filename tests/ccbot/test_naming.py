"""Tests for naming._sanitize."""

import pytest

from ccbot.naming import _sanitize


class TestSanitize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("frontend-redesign", "frontend-redesign"),
            ("FRONTEND REDESIGN", "frontend-redesign"),
            ("auth_backend!!", "auth-backend"),
            ("  scrape-linkedin  ", "scrape-linkedin"),
            ("`linkedin-scraper`", "linkedin-scraper"),
            ('"my session"', "my-session"),
            ("ok\nextra fluff", "ok"),
        ],
    )
    def test_normal_inputs(self, raw: str, expected: str) -> None:
        assert _sanitize(raw) == expected

    def test_too_long_rejected(self) -> None:
        # Output regex caps at 32 chars total.
        assert _sanitize("a" * 50) == ""

    def test_empty_returns_empty(self) -> None:
        assert _sanitize("") == ""

    def test_starts_with_digit_rejected(self) -> None:
        # Regex requires leading [a-z].
        assert _sanitize("123-abc") == ""

    def test_double_dashes_collapsed(self) -> None:
        assert _sanitize("foo----bar") == "foo-bar"
