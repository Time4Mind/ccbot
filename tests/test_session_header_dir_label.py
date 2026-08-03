"""Regression tests: an active session's header shows its directory name
(truncated to 7 chars + ellipsis) instead of the redundant "active" state
label — a card you're looking at is active by definition. Non-active
states (idle/archived/completed/lost) still show the real state.
"""

from __future__ import annotations

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

    def test_short_dirname_untouched(self) -> None:
        sess = _session(workdir="/root/ccbot")
        text = _render_card(sess, CardState())
        assert "· ccbot" in text

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
