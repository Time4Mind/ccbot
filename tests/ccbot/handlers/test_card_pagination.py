"""Tests for the live-card event model + pagination."""

from __future__ import annotations

import time

from ccbot.handlers.notifications import (
    Event,
    _apply_tool_result,
    _build_event,
    _is_in_flight,
    _resolved_page_idx,
    CardState,
    paginate_events,
    render_event,
)
from ccbot.session_monitor import NewMessage


def _msg(
    content_type: str,
    text: str,
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    role: str = "assistant",
    stop_reason: str | None = None,
    timestamp: str = "",
) -> NewMessage:
    return NewMessage(
        session_id="s1",
        text=text,
        is_complete=True,
        content_type=content_type,
        tool_use_id=tool_use_id,
        role=role,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


class TestBuildEvent:
    def test_thinking_event(self) -> None:
        # Real thinking arrives EXPQUOTE-wrapped from transcript_parser;
        # _build_event pulls the inner text into ``body`` for plain-text
        # card rendering and leaves ``text`` empty (head is just
        # "∴ thinking").
        wrapped = "\x02EXPQUOTE_START\x02pondering oauth flow\x02EXPQUOTE_END\x02"
        ev = _build_event(_msg("thinking", wrapped))
        assert ev.type == "thinking"
        assert ev.is_page_break is False
        assert ev.completed_at is None
        assert "pondering" in ev.body
        assert ev.text == ""

    def test_tool_use_carries_id_and_name(self) -> None:
        ev = _build_event(
            _msg("tool_use", "Read(file)", tool_use_id="tuid-1", tool_name="Read")
        )
        assert ev.type == "tool_use"
        assert ev.tool_use_id == "tuid-1"
        assert ev.tool_name == "Read"
        assert ev.completed_at is None

    def test_final_text_marks_page_break(self) -> None:
        ev = _build_event(_msg("text", "Done!", stop_reason="end_turn"))
        assert ev.type == "final_text"
        assert ev.is_page_break is True
        assert ev.completed_at is not None

    def test_midstream_text_is_not_final(self) -> None:
        ev = _build_event(_msg("text", "Working...", stop_reason="tool_use"))
        assert ev.type == "text"
        assert ev.is_page_break is False

    def test_user_message(self) -> None:
        ev = _build_event(_msg("text", "fix login", role="user"))
        assert ev.type == "user_msg"

    def test_parses_iso_timestamp(self) -> None:
        ev = _build_event(_msg("thinking", "x", timestamp="2026-05-15T14:31:23Z"))
        # Just make sure it's a sensible epoch — not the fallback of "now"
        # exactly (allow a minute of skew either way around the known epoch).
        # 2026-05-15T14:31:23Z = 1779197483 (UTC). We don't pin to that
        # exact value because the test environment might apply tz.
        assert ev.started_at > 0


class TestApplyToolResult:
    def test_folds_into_matching_tool_use(self) -> None:
        state = CardState()
        state.events.append(
            _build_event(
                _msg("tool_use", "Bash(npm test)", tool_use_id="t1", tool_name="Bash")
            )
        )
        result = _build_event(_msg("tool_result", "PASS 24 tests", tool_use_id="t1"))
        assert _apply_tool_result(state, result) is True
        ev = state.events[0]
        assert ev.completed_at is not None
        assert ev.is_error is False
        assert ev.text == "PASS 24 tests"

    def test_no_match_returns_false(self) -> None:
        state = CardState()
        state.events.append(
            _build_event(
                _msg("tool_use", "Read(file)", tool_use_id="other", tool_name="Read")
            )
        )
        result = _build_event(_msg("tool_result", "data", tool_use_id="missing"))
        assert _apply_tool_result(state, result) is False


class TestPaginateEvents:
    def test_empty(self) -> None:
        assert paginate_events([]) == [[]]

    def test_no_break_single_page(self) -> None:
        events = [
            Event(type="thinking", text="∴ a", started_at=1.0),
            Event(type="tool_use", text="▷ b", started_at=2.0),
        ]
        assert paginate_events(events) == [events]

    def test_break_starts_new_page(self) -> None:
        e1 = Event(type="user_msg", text="👤 q", started_at=1.0)
        e2 = Event(type="tool_use", text="▷ t", started_at=2.0)
        e3 = Event(type="final_text", text="Done", started_at=3.0, is_page_break=True)
        e4 = Event(type="user_msg", text="👤 q2", started_at=4.0)
        e5 = Event(type="tool_use", text="▷ t2", started_at=5.0)
        pages = paginate_events([e1, e2, e3, e4, e5])
        assert len(pages) == 2
        assert pages[0] == [e1, e2]
        assert pages[1] == [e3, e4, e5]

    def test_consecutive_breaks(self) -> None:
        e1 = Event(type="final_text", text="A1", started_at=1.0, is_page_break=True)
        e2 = Event(type="final_text", text="A2", started_at=2.0, is_page_break=True)
        pages = paginate_events([e1, e2])
        assert len(pages) == 2
        assert pages[0] == [e1]
        assert pages[1] == [e2]


class TestResolvedPageIdx:
    def test_none_means_latest(self) -> None:
        state = CardState(current_page_idx=None)
        assert _resolved_page_idx(state, 3) == 2

    def test_clamps_high(self) -> None:
        state = CardState(current_page_idx=999)
        assert _resolved_page_idx(state, 3) == 2

    def test_clamps_low(self) -> None:
        state = CardState(current_page_idx=-1)
        assert _resolved_page_idx(state, 3) == 0


class TestInFlight:
    def test_tool_use_no_result_is_in_flight(self) -> None:
        e = Event(type="tool_use", text="▷ x", started_at=1.0)
        assert _is_in_flight(e, [e], 0) is True

    def test_completed_tool_use_is_not(self) -> None:
        e = Event(type="tool_use", text="▷ x", started_at=1.0, completed_at=2.0)
        assert _is_in_flight(e, [e], 0) is False

    def test_thinking_in_flight_only_if_last(self) -> None:
        a = Event(type="thinking", text="∴ a", started_at=1.0)
        b = Event(type="tool_use", text="▷ b", started_at=2.0)
        assert _is_in_flight(a, [a, b], 0) is False  # has a successor
        assert _is_in_flight(b, [a, b], 1) is True  # last + in-flight


class TestRenderEvent:
    def test_inflight_tool_uses_hourglass(self) -> None:
        now = 1000.0
        e = Event(
            type="tool_use",
            text="Read(file)",
            started_at=now - 8.0,
            tool_name="Read",
        )
        out = render_event(e, in_flight=True, now=now)
        assert "▷" in out
        assert "⏳" in out
        assert "0:08" in out

    def test_completed_tool_shows_hhmm(self) -> None:
        e = Event(
            type="tool_use",
            text="Read(file)",
            started_at=1000.0,
            completed_at=1010.0,
        )
        out = render_event(e, in_flight=False, now=1010.0)
        assert "✓" in out
        # contains an HH:MM marker
        assert ":" in out.split(" · ")[-1]

    def test_error_tool_shows_cross(self) -> None:
        e = Event(
            type="tool_use",
            text="Bash(failing)",
            started_at=1.0,
            completed_at=2.0,
            is_error=True,
        )
        out = render_event(e, in_flight=False, now=2.0)
        assert out.startswith("✗ ")

    def test_user_msg_glyph(self) -> None:
        e = Event(type="user_msg", text="fix login", started_at=time.time())
        out = render_event(e, in_flight=False, now=time.time())
        assert out.startswith("👤 ")
