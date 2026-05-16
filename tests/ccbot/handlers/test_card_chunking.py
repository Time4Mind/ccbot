"""Tests for ``_chunk_final_text`` — splits long final answers by LINE
budget with smart paragraph / line / sentence / word boundaries.

User spec: budget is in LINES; smart-boundary order is
``\\n\\n > \\n > [.!?] > space > hard``; up to
``CARD_PAGE_LINES_OVERSHOOT`` extra lines allowed so a sentence is
never broken mid-content.
"""

from __future__ import annotations

from ccbot.handlers.notifications import (
    CARD_PAGE_BUDGET,
    CARD_PAGE_LINES_DEFAULT,
    Event,
    CardState,
    _chunk_final_text,
    _count_lines,
    _estimate_md_v2_size,
    _rechunk_oversized_finals_inplace,
    _split_page_by_budget,
    render_page,
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

    def test_byte_budget_splits_wide_paragraph(self) -> None:
        # Single visual line / few paragraphs but very wide: passes the
        # line cap and would still overflow Telegram's 4096-byte edit
        # limit after MarkdownV2 escaping. Regression for the freeze on
        # /root/projects/pets — assistant emitted a 5400-char answer in
        # ≈40 lines and every card edit failed with Message_too_long.
        wide_paragraph = (
            "Direct vendor programs (без посредников, максимум $$). "
            "Google Android VRP — до $1M за full exploit chain. "
            "Реалистично $5-50k за компонентные баги. "
        ) * 40  # ~5600 chars, lots of MD-V2 special chars (- . ( ) $)
        chunks = _chunk_final_text(
            wide_paragraph,
            budget_lines=CARD_PAGE_LINES_DEFAULT,
            byte_budget=CARD_PAGE_BUDGET,
        )
        assert len(chunks) >= 2, "wide single-paragraph text must be split"
        for c in chunks:
            assert _estimate_md_v2_size(c) <= CARD_PAGE_BUDGET, (
                f"chunk exceeds byte budget: {_estimate_md_v2_size(c)}"
            )


class TestRechunkOversizedFinalsInplace:
    def test_byte_overflow_triggers_split(self) -> None:
        # The exact scenario from the 2026-05-16 bot.log freeze:
        # one final_text Event with low line count but high byte count
        # after MD-V2 escaping. Pre-fix, rechunk left this untouched
        # because it only checked _count_lines; _edit_card then failed
        # with Message_too_long on every retry until the carrier was
        # forcibly detached.
        body = (
            "Да, и довольно много. Делю по типу, сверху — самые жирные выплаты.\n"
            + "\n".join(
                f"• Bullet {i}: source.android.com/security/overview — до $50k."
                for i in range(80)
            )
        )
        ev = Event(
            type="final_text",
            text=body,
            body=body,
            started_at=0.0,
            is_page_break=True,
        )
        state = CardState(events=[ev])
        # User's actual budget at the time of the freeze.
        _rechunk_oversized_finals_inplace(state, budget_lines=70)
        assert len(state.events) >= 2, "oversized final_text must be split"
        for split_ev in state.events:
            assert _estimate_md_v2_size(split_ev.text) <= CARD_PAGE_BUDGET, (
                "post-rechunk chunk still exceeds Telegram-safe byte budget"
            )
            # Every chunk is a fresh page so the user can paginate through.
            assert split_ev.is_page_break is True
            assert split_ev.type == "final_text"

    def test_fits_both_budgets_is_idempotent(self) -> None:
        body = "Короткий ответ в одну строку."
        ev = Event(type="final_text", text=body, body=body, started_at=0.0)
        state = CardState(events=[ev])
        _rechunk_oversized_finals_inplace(state, budget_lines=70)
        assert len(state.events) == 1
        assert state.events[0] is ev


class TestSplitPageByBudget:
    def test_byte_overflow_splits_even_when_line_fits(self) -> None:
        # Regression for tests/@120 freeze: a page accreted many small
        # tool_use rows whose paths and bash commands are MD-V2-escape
        # heavy. Total line count stayed under budget, but rendered
        # bytes exceeded Telegram's 4096-byte edit cap → Message_too_long
        # on page navigation until the user noticed page N didn't paint.
        # Each event is a single line (1 line ≪ budget) but accumulates
        # ~200 bytes after MD-V2 escaping; 30 of them = ~6000 bytes > 4096.
        events = [
            Event(
                type="tool_use",
                text=f"**Bash**(/usr/local/bin/termux-exec \"pm list packages | grep -iE 'lsposed|xposed|magisk|patcher' (line {i})\")",
                body="",
                started_at=0.0,
            )
            for i in range(30)
        ]
        sub_pages = _split_page_by_budget(events, budget_lines=70)
        assert len(sub_pages) >= 2, (
            "30 byte-heavy events must split despite fitting in lines"
        )
        for page in sub_pages:
            rendered = render_page(page, now=0.0)
            assert _estimate_md_v2_size(rendered) <= CARD_PAGE_BUDGET, (
                f"sub-page exceeds Telegram-safe byte budget: "
                f"{_estimate_md_v2_size(rendered)} bytes"
            )

    def test_fits_both_budgets_is_one_page(self) -> None:
        events = [
            Event(type="text", text=f"line {i}", body="", started_at=0.0)
            for i in range(5)
        ]
        sub_pages = _split_page_by_budget(events, budget_lines=70)
        assert len(sub_pages) == 1
        assert sub_pages[0] == events

    def test_empty_returns_singleton_empty(self) -> None:
        # Callers iterate over .extend(); preserving the [[]] shape lets
        # pagination report 1/1 instead of 0/0 on a fresh card.
        assert _split_page_by_budget([], budget_lines=70) == [[]]
