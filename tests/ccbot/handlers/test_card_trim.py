"""Tests for ``_trim_page_events`` — drops middle events from a page so the
rendered output fits the user's line-budget (with ±5 lines overshoot).

Always preserves the anchor (first event = page answer / page break) and
the freshest tail events (latest signal).
"""

from __future__ import annotations

from ccbot.handlers.notifications import (
    CARD_PAGE_LINES_OVERSHOOT,
    Event,
    _count_lines,
    _trim_page_events,
    render_page,
)


def _ev(type_: str, text: str, started_at: float = 1.0, **kw: object) -> Event:
    return Event(type=type_, text=text, started_at=started_at, **kw)  # type: ignore[arg-type]


class TestTrimPageEvents:
    def test_empty_in_empty_out(self) -> None:
        assert _trim_page_events([], 30) == []

    def test_under_budget_unchanged(self) -> None:
        events = [
            _ev("user_msg", "👤 hi"),
            _ev("tool_use", "Bash", started_at=2.0),
        ]
        out = _trim_page_events(events, 100)
        assert out == events

    def test_drops_middle_events_to_fit_budget(self) -> None:
        # Anchor (final_text) + 50 chatty tool_use's + tail thinking.
        # Budget too small for all 52 events; expect anchor + tail kept,
        # middles dropped.
        anchor = _ev(
            "final_text",
            "ANCHOR\nline2\nline3",
            started_at=1.0,
            is_page_break=True,
        )
        middles = [
            _ev("tool_use", f"Bash_{i}", started_at=2.0 + i)
            for i in range(50)
        ]
        tail = _ev("thinking", "", started_at=100.0)
        events = [anchor, *middles, tail]
        out = _trim_page_events(events, 10)
        # Anchor preserved as the first event.
        assert out[0] is anchor
        # Tail is in the kept slice.
        assert tail in out
        # Some middle events dropped.
        assert len(out) < len(events)

    def test_keeps_at_least_anchor(self) -> None:
        # Anchor so big it doesn't fit at all → still kept (without
        # the anchor the page has no context whatsoever).
        anchor = _ev(
            "final_text",
            "\n".join([f"line {i}" for i in range(200)]),
            started_at=1.0,
            is_page_break=True,
        )
        out = _trim_page_events([anchor, _ev("tool_use", "Read")], 10)
        assert out[0] is anchor

    def test_renders_under_budget_lines_after_trim(self) -> None:
        # Page with many tool events, each rendering as multiple lines.
        # After trim, rendered line count ≤ budget + overshoot.
        events = [
            _ev("final_text", "ANSWER", started_at=1.0, is_page_break=True),
        ] + [
            _ev(
                "tool_use",
                "tool",
                started_at=2.0 + i,
                body="\n".join(["body"] * 5),  # multi-line body
            )
            for i in range(40)
        ]
        budget = 15
        out = _trim_page_events(events, budget)
        rendered = render_page(out, now=2.0)
        actual_lines = _count_lines(rendered)
        assert actual_lines <= budget + CARD_PAGE_LINES_OVERSHOOT + 5  # render_page joins with \\n\\n\\n
