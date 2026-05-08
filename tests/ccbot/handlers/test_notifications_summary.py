"""Tests for notifications._summary_for_push."""

import pytest

from ccbot.handlers.notifications import _summary_for_push


class TestSummaryForPush:
    def test_empty_string(self) -> None:
        assert _summary_for_push("") == "task complete"

    def test_whitespace_only(self) -> None:
        assert _summary_for_push("   \n\n  ") == "task complete"

    def test_first_sentence(self) -> None:
        text = "Implemented login. Added unit tests. Cleaned up imports."
        assert _summary_for_push(text) == "Implemented login."

    def test_first_paragraph_no_punctuation(self) -> None:
        text = "Just a single line answer"
        assert _summary_for_push(text) == "Just a single line answer"

    def test_skips_short_heading(self) -> None:
        text = "## Summary\n\nFixed the auth bug. Wrote a test."
        assert _summary_for_push(text) == "Fixed the auth bug."

    def test_strips_bullet_markers(self) -> None:
        text = "- did the thing"
        assert _summary_for_push(text) == "did the thing"

    def test_long_text_caps_at_limit(self) -> None:
        text = "x" * 500
        out = _summary_for_push(text, limit=50)
        assert len(out) <= 50
        assert out.endswith("…")

    def test_preserves_punctuation(self) -> None:
        text = "Built the fix! Tests pass. Ready for review."
        out = _summary_for_push(text)
        assert out == "Built the fix!"

    def test_single_long_paragraph(self) -> None:
        text = "no sentence terminator here just a long explanation that runs on and on"
        out = _summary_for_push(text, limit=40)
        # Falls back to length cap when no terminator within range.
        assert len(out) <= 40

    def test_question_terminator(self) -> None:
        text = "Does this match? Yes."
        # We accept "?" terminators only when not the very first chars.
        # "Does this match?" is 16 chars — below the 20-char floor —
        # so we fall through to the length cap and keep the whole text.
        out = _summary_for_push(text)
        assert out.startswith("Does this match")

    @pytest.mark.parametrize(
        "raw,expected_start",
        [
            ("# Title\n\nReal content here.", "Real content here"),
            ("> quoted intro\nMore text.", "quoted intro"),
            ("* bullet one\n* bullet two", "bullet one"),
        ],
    )
    def test_strips_various_markers(self, raw: str, expected_start: str) -> None:
        out = _summary_for_push(raw)
        assert out.startswith(expected_start)
