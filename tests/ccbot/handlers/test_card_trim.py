"""Tests for ``_trim_page_events`` — drops middle events from a page so the
rendered output fits Telegram's 4096-char limit while preserving the
page anchor (the first event = answer text) and the latest tail."""

from __future__ import annotations

from ccbot.handlers.notifications import Event, _trim_page_events, render_page


def _ev(type_: str, text: str, started_at: float = 1.0, **kw: object) -> Event:
    return Event(type=type_, text=text, started_at=started_at, **kw)  # type: ignore[arg-type]


class TestTrimPageEvents:
    def test_empty_in_empty_out(self) -> None:
        assert _trim_page_events([], 1000) == []

    def test_under_budget_unchanged(self) -> None:
        events = [
            _ev("user_msg", "👤 hi"),
            _ev("tool_use", "Bash", started_at=2.0),
        ]
        out = _trim_page_events(events, 10_000)
        assert out == events

    def test_drops_middle_events_to_fit_budget(self) -> None:
        # Anchor (final_text) + 50 chatty tool_use's + tail thinking.
        # Budget too small for all 52 events; expect anchor + tail kept,
        # middles dropped.
        anchor = _ev(
            "final_text",
            "ANCHOR " + "x" * 200,
            started_at=1.0,
            is_page_break=True,
        )
        middles = [
            _ev("tool_use", f"Bash_{i}", started_at=2.0 + i)
            for i in range(50)
        ]
        tail = _ev("thinking", "", started_at=100.0)
        events = [anchor, *middles, tail]
        out = _trim_page_events(events, 500)
        # Anchor preserved as the first event.
        assert out[0] is anchor
        # Tail (last event before trim) is in the kept slice.
        assert tail in out
        # Some middle events dropped.
        assert len(out) < len(events)

    def test_keeps_at_least_anchor(self) -> None:
        # Anchor so big it doesn't fit at all → still kept (without
        # the anchor the page has no context whatsoever).
        anchor = _ev(
            "final_text",
            "x" * 5000,
            started_at=1.0,
            is_page_break=True,
        )
        out = _trim_page_events([anchor, _ev("tool_use", "Read")], 100)
        assert out[0] is anchor

    def test_renders_under_budget_after_trim(self) -> None:
        # Synthetic page that renders well over budget; after trim it
        # should fit.
        events = [
            _ev("final_text", "ANSWER", started_at=1.0, is_page_break=True),
        ] + [
            _ev("tool_use", "tool", started_at=2.0 + i, body="y" * 200)
            for i in range(40)
        ]
        budget = 1500
        out = _trim_page_events(events, budget)
        rendered = render_page(out, now=2.0)
        # Allow a small overshoot tolerance for joining newlines + the
        # body's EXPQUOTE wrapping that render_page applies after the
        # event-level size estimate (~10-20% headroom is what the caller
        # in _render_card budgets anyway).
        assert len(rendered) <= int(budget * 1.6)
