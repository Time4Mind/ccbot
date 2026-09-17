"""Regression tests: an active session's header shows its directory name
(truncated to 7 chars + ellipsis) instead of the redundant "active" state
label — a card you're looking at is active by definition. Non-active
states (idle/archived/completed/lost) still show the real state.
"""

from __future__ import annotations

import pytest

from ccbot.handlers.card_model import CardState, Event, _render_card
from ccbot.handlers.switcher import build_session_preview
from ccbot.session_models import Session


def _session(**overrides: object) -> Session:
    defaults: dict[str, object] = {
        "id": "abc12345",
        "name": "my-session",
        "window_id": "@1",
        "workdir": "/root/projects/claude-plugins",
        "state": "active",
    }
    defaults.update(overrides)
    return Session(**defaults)  # type: ignore[arg-type]


class TestCardHeaderDirLabel:
    def test_active_session_shows_truncated_dirname(self) -> None:
        sess = _session()
        text = _render_card(sess, CardState())
        assert "claude-…" in text
        assert "· active" not in text
        assert not any(
            marker in text.splitlines()[0]
            for marker in (
                "🟦",
                "🟩",
                "🟨",
                "🟧",
                "🟥",
                "🟪",
                "🟫",
                "⬛",
                "🔵",
                "🟢",
                "🟣",
                "🟠",
            )
        )

    def test_short_dirname_untouched(self) -> None:
        sess = _session(workdir="/root/ccbot")
        text = _render_card(sess, CardState())
        assert "· ccbot" in text

    def test_exact_home_directory_is_rendered_as_tilde(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/Users/tester")
        sess = _session(workdir="/Users/tester")

        text = _render_card(sess, CardState())

        assert "· ~" in text.splitlines()[0]
        assert "· tester" not in text.splitlines()[0]

    def test_idle_session_keeps_state_label(self) -> None:
        sess = _session(state="idle")
        text = _render_card(sess, CardState())
        assert "· idle" in text
        assert "claude-" not in text

    def test_no_workdir_falls_back_to_empty_label(self) -> None:
        sess = _session(workdir="")
        text = _render_card(sess, CardState())
        assert "*my-session* · " in text.split("\n")[0]


class TestVoicePendingMarker:
    def test_marker_is_trailing_user_row_not_header(self) -> None:
        sess = _session()
        state = CardState()
        state.events.append(
            Event(type="final_text", text="previous answer", started_at=1.0)
        )
        state.voice_pending = True
        text = _render_card(sess, state)
        assert "🎙" not in text.splitlines()[0]
        assert "👤 🎙 Voice message is being transcribed…" in text
        assert text.index("previous answer") < text.index("👤 🎙")

    def test_marker_absent_when_not_pending(self) -> None:
        sess = _session()
        state = CardState()
        text = _render_card(sess, state)
        assert "🎙" not in text


class TestPaneStatusPlacement:
    def test_working_status_only_appears_at_bottom_of_latest_page(self) -> None:
        state = CardState(pane_status="Working · 1 background terminal running")
        state.events.extend(
            [
                Event(
                    type="user_msg",
                    text="first request",
                    started_at=1.0,
                    is_page_break=True,
                ),
                Event(type="final_text", text="first answer", started_at=2.0),
                Event(
                    type="user_msg",
                    text="second request",
                    started_at=3.0,
                    is_page_break=True,
                ),
            ]
        )

        state.current_page_idx = 0
        old_page = _render_card(_session(), state)
        assert "background terminal running" not in old_page

        state.current_page_idx = 2
        latest_page = _render_card(_session(), state)
        assert latest_page.rstrip().endswith(
            "• Working · 1 background terminal running"
        )
        assert latest_page.index("second request") < latest_page.index("• Working")


def test_card_omits_background_panel_but_keeps_context_after_media_anchor() -> None:
    state = CardState(
        context_pct=42,
        pane_status="Working · 1 background terminal running",
    )
    state.events.append(Event(type="final_text", text="answer", started_at=1.0))

    text = _render_card(_session(), state, user_id=1)
    body = text[: state.media_anchor_offset]
    service_tail = text[state.media_anchor_offset :]

    assert "answer" in body
    assert "context:" not in body
    assert "─── фон ───" not in body
    assert body.rstrip().endswith("• Working · 1 background terminal running")
    assert "context: 42%" in service_tail
    assert "─── фон ───" not in service_tail
    assert "background-session" not in service_tail
    assert "background terminal running" not in service_tail


class TestSwitcherPreviewDirLabel:
    def test_active_preview_shows_dirname(self) -> None:
        sess = _session()
        text = build_session_preview(sess)
        assert "claude-…" in text
        assert "· active" not in text

    def test_archived_preview_keeps_state_label(self) -> None:
        sess = _session(state="archived", window_id="")
        text = build_session_preview(sess)
        assert "· archived" in text


class TestSessionDirLabel:
    def test_truncates_to_seven_plus_ellipsis(self) -> None:
        sess = _session(workdir="/root/projects/claude-plugins")
        assert sess.dir_label == "claude-…"

    def test_short_name_untouched(self) -> None:
        sess = _session(workdir="/root/ccbot")
        assert sess.dir_label == "ccbot"

    def test_exactly_seven_chars_untouched(self) -> None:
        sess = _session(workdir="/root/projects/1234567")
        assert sess.dir_label == "1234567"

    def test_trailing_slash_ignored(self) -> None:
        sess = _session(workdir="/root/projects/claude-plugins/")
        assert sess.dir_label == "claude-…"

    def test_empty_workdir(self) -> None:
        sess = _session(workdir="")
        assert sess.dir_label == ""
