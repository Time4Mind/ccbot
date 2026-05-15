"""Tests for ``_chunk_final_text`` — splits long final answers at paragraph /
line / hard boundaries so each chunk fits inside one Telegram message."""

from __future__ import annotations

from ccbot.handlers.notifications import CARD_PAGE_BUDGET, _chunk_final_text


class TestChunkFinalText:
    def test_empty_returns_empty(self) -> None:
        assert _chunk_final_text("") == []

    def test_short_returns_single_chunk(self) -> None:
        assert _chunk_final_text("hello world") == ["hello world"]

    def test_under_budget_unchanged(self) -> None:
        text = "x" * (CARD_PAGE_BUDGET - 1)
        assert _chunk_final_text(text) == [text]

    def test_paragraph_break_preferred(self) -> None:
        # Two ~half-budget paragraphs joined by \n\n. Cut should land
        # exactly at the paragraph boundary, not mid-paragraph.
        half = "a" * (CARD_PAGE_BUDGET // 2)
        text = f"{half}\n\n{half}"
        chunks = _chunk_final_text(text)
        assert len(chunks) == 2
        assert chunks[0] == half
        assert chunks[1] == half

    def test_line_break_when_no_paragraph(self) -> None:
        # Single-line breaks only — cut should land at a line edge.
        line = "y" * (CARD_PAGE_BUDGET // 4)
        text = f"{line}\n{line}\n{line}\n{line}\n{line}"
        chunks = _chunk_final_text(text)
        assert len(chunks) >= 2
        # All chunks except the last should consist of WHOLE lines —
        # i.e. their content split on \n equals back to lines (no
        # partial-line tail). The hard-cut fallback would leave a
        # truncated last line in a non-last chunk.
        for c in chunks[:-1]:
            for piece in c.split("\n"):
                assert piece == line

    def test_hard_cut_when_no_breaks(self) -> None:
        # No \n at all — cut falls back to hard char cut at budget.
        text = "z" * (CARD_PAGE_BUDGET * 2 + 100)
        chunks = _chunk_final_text(text)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= CARD_PAGE_BUDGET
        # Roundtrips the content (no chars dropped, except whitespace strip
        # on chunk boundaries — no whitespace here).
        assert "".join(chunks) == text

    def test_custom_budget(self) -> None:
        chunks = _chunk_final_text("a\n\nb\n\nc\n\nd", budget=4)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= 4
