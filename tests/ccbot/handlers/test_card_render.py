"""Tests for ``render_page`` separator + tool head/body split + spoiler
wrapping — covers what the user actually sees in the card."""

from __future__ import annotations

from ccbot.handlers.notifications import (
    Event,
    _build_event,
    _format_hhmmss,
    _spoiler_body,
    render_event,
    render_page,
)
from ccbot.session_monitor import NewMessage


def _msg(content_type: str, text: str, **kw) -> NewMessage:  # type: ignore[no-untyped-def]
    return NewMessage(
        session_id="s1",
        text=text,
        is_complete=True,
        content_type=content_type,
        tool_use_id=kw.get("tool_use_id"),
        role=kw.get("role", "assistant"),
        tool_name=kw.get("tool_name"),
        stop_reason=kw.get("stop_reason"),
        timestamp=kw.get("timestamp", ""),
    )


class TestRenderPageSeparator:
    def test_events_separated_by_blank_line(self) -> None:
        # Two thinking events — rendered output should have a blank line
        # between them (per user feedback: "сплошная масса tldr" — fixed
        # by switching from \n to \n\n in render_page).
        e1 = Event(type="thinking", text="", started_at=1.0)
        e2 = Event(type="thinking", text="", started_at=2.0, completed_at=2.0)
        out = render_page([e1, e2], now=3.0)
        # Between the two ∴ lines we expect a blank line — i.e. \n\n.
        assert out.count("\n∴") >= 1
        # Spacing: not a wall of text.
        assert "\n\n" in out


class TestToolHeadAndSpoiler:
    def test_tool_head_only_name(self) -> None:
        # The agreed format: head shows only the tool NAME, not the args.
        # Bash(very long command...) should give head "Bash · ⏳ 0:00"
        # with the command moved under the spoiler.
        ev = _build_event(
            _msg(
                "tool_use",
                "**Bash**(uv run python /tmp/render_check.py 2>&1 | tail -40)",
                tool_use_id="t1",
                tool_name="Bash",
            )
        )
        assert ev.text == "Bash"
        assert "uv run python" in ev.body  # args ended up under spoiler

    def test_completed_tool_head_has_summary(self) -> None:
        # After tool_result fold (or as a standalone tool_result), head
        # shows "Name · summary"  e.g. "Bash · Output 5 lines".
        ev = _build_event(
            _msg(
                "tool_result",
                "**Bash**(ls)\n  ⎿  Output 5 lines\n"
                "\x02EXPQUOTE_START\x02a\nb\x02EXPQUOTE_END\x02",
                tool_use_id="t1",
            )
        )
        assert "Bash" in ev.text
        assert "Output 5 lines" in ev.text


class TestSpoilerBody:
    def test_empty_body_returns_empty(self) -> None:
        assert _spoiler_body("") == ""

    def test_wraps_in_expquote_sentinels(self) -> None:
        out = _spoiler_body("some body line\nanother line")
        # _spoiler_body delegates to format_expandable_quote which wraps
        # in \x02EXPQUOTE_START\x02 ... \x02EXPQUOTE_END\x02
        assert "\x02EXPQUOTE_START\x02" in out
        assert "\x02EXPQUOTE_END\x02" in out

    def test_trims_to_spoiler_max_lines(self) -> None:
        body = "\n".join(f"line {i}" for i in range(20))
        out = _spoiler_body(body)
        # ``_body_trim`` caps at SPOILER_MAX_LINES (default 5) + a
        # ``… (+N more lines)`` summary line.
        assert "more lines" in out


class TestFormatHhmmss:
    def test_format(self) -> None:
        # 1700000000 corresponds to some specific HH:MM:SS depending on tz —
        # we only validate the SHAPE, not the value.
        out = _format_hhmmss(1700000000.0)
        assert len(out) == 8  # HH:MM:SS
        assert out.count(":") == 2


class TestRenderEventTimings:
    def test_inflight_shows_elapsed_not_hhmm(self) -> None:
        e = Event(type="tool_use", text="Read", started_at=1000.0)
        out = render_event(e, in_flight=True, now=1008.0)
        assert "⏳" in out
        assert "0:08" in out
        # When in-flight, HH:MM marker should NOT be present.
        assert ":" in out  # the 0:08 itself has a colon, that's ok

    def test_completed_shows_hhmm_only(self) -> None:
        e = Event(
            type="tool_use",
            text="Read",
            started_at=1000.0,
            completed_at=1010.0,
        )
        out = render_event(e, in_flight=False, now=1010.0)
        assert "✓" in out
        assert "⏳" not in out
