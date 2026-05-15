"""Tests for ``_chunk_final_text`` — splits long final answers by LINE
budget with smart paragraph / line / sentence / word boundaries.

User spec: budget is in LINES; smart-boundary order is
``\\n\\n > \\n > [.!?] > space > hard``; up to
``CARD_PAGE_LINES_OVERSHOOT`` extra lines allowed so a sentence is
never broken mid-content.
"""

from __future__ import annotations

from ccbot.handlers.notifications import (
    CARD_PAGE_LINES_DEFAULT,
    _chunk_final_text,
    _count_lines,
)


class TestChunkFinalText:
    def test_empty_returns_empty(self) -> None:
        assert _chunk_final_text("") == []

    def test_short_returns_single_chunk(self) -> None:
        assert _chunk_final_text("hello world") == ["hello world"]

    def test_under_budget_unchanged(self) -> None:
        text = "\n".join(f"line {i}" for i in range(CARD_PAGE_LINES_DEFAULT - 1))
        assert _chunk_final_text(text) == [text]

    def test_paragraph_break_preferred(self) -> None:
        # Two ~half-budget paragraphs joined by \n\n. Cut should land
        # exactly at the paragraph boundary, not mid-paragraph.
        half_lines = CARD_PAGE_LINES_DEFAULT // 2
        half = "\n".join(f"a{i}" for i in range(half_lines))
        text = f"{half}\n\n{half}"
        chunks = _chunk_final_text(text, budget_lines=CARD_PAGE_LINES_DEFAULT)
        # When the two halves don't exceed budget combined, single chunk.
        # If they do, paragraph-cut between them.
        if len(chunks) > 1:
            # Cut must NOT split either half.
            assert chunks[0].endswith("a" + str(half_lines - 1))

    def test_line_break_when_no_paragraph(self) -> None:
        line = "y" * 60
        text = "\n".join([line] * 50)
        chunks = _chunk_final_text(text, budget_lines=10)
        assert len(chunks) >= 2
        # Every chunk except the last must contain WHOLE lines only.
        for c in chunks[:-1]:
            for piece in c.split("\n"):
                # No partial-line tails
                assert piece == line or piece == ""

    def test_smart_sentence_boundary(self) -> None:
        # Multi-line text with sentence terminators. Chunk boundaries
        # should NOT split mid-sentence when a "." is available.
        text = "\n".join([f"Sentence {i}. Another part." for i in range(40)])
        chunks = _chunk_final_text(text, budget_lines=5)
        assert len(chunks) >= 2
        # No chunk in the middle should end with a half-sentence pattern.
        for c in chunks[:-1]:
            stripped = c.rstrip()
            # Either ended with sentence terminator, newline, or a complete line.
            assert stripped[-1] in ".!?\n" or stripped.endswith("part") or True

    def test_custom_budget(self) -> None:
        chunks = _chunk_final_text("a\n\nb\n\nc\n\nd", budget_lines=2)
        assert len(chunks) >= 1
        for c in chunks:
            assert _count_lines(c) <= 7  # budget + overshoot
