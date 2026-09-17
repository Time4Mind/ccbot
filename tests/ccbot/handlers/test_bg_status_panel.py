"""Regression: ``bg_status.render_panel`` joins rows with a hard line
break (``  \\n``) so the rich-message parser doesn't collapse the panel
header and badge rows onto one line.

Field bug from the 2026-06-13 screenshot at
``.ccbot-inbox/1781374419-…`` — single ``\\n`` is a CommonMark soft
break (= space), so the live card showed
``─── фон ─── ⬛ session-name · 50%`` on one row instead of the
header on a row above each badge.
"""

from __future__ import annotations

import json

import pytest

from ccbot.handlers import bg_status
from ccbot.session import session_manager
from ccbot.session_models import Session


@pytest.fixture
def isolated_bg():
    """Snapshot + restore the module-level ``_bg`` dict so tests don't leak."""
    snapshot = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    bg_status._bg.clear()
    yield
    bg_status._bg.clear()
    bg_status._bg.update(snapshot)


def _seed_session(sid: str, name: str = "bg-sess") -> Session:
    sess = Session(
        id=sid,
        name=name,
        window_id="@bg",
        workdir="/tmp/x",
        state="active",
    )
    session_manager.sessions[sid] = sess
    return sess


class TestPanelHardBreaks:
    def test_header_and_badge_on_separate_lines(
        self, isolated_bg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid = "bgsess01"
        _seed_session(sid)
        try:
            entry = bg_status._entry(42, sid)
            entry.status = "working"
            entry.context_pct = 50
            entry.last_change = 100.0

            out = bg_status.render_panel(42)

            # Header line.
            assert "─── фон ───" in out
            # Badge line.
            assert "bg-sess" in out
            # Hard line break between header and badge — two trailing
            # spaces before the newline so CommonMark renders them as
            # separate lines (single ``\n`` is a soft break = space).
            assert "  \n" in out
            assert "─── фон ───  \n" in out
            # And NOT collapsed onto a single line.
            assert "─── фон ─── ⬛" not in out
            assert "· 50%" in out
            assert "context" not in out
        finally:
            session_manager.sessions.pop(sid, None)

    def test_multiple_rows_each_get_hard_break(
        self, isolated_bg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid_a = "bgsess0a"
        sid_b = "bgsess0b"
        _seed_session(sid_a, "alpha")
        _seed_session(sid_b, "beta")
        try:
            e_a = bg_status._entry(42, sid_a)
            e_a.status = "working"
            e_a.last_change = 100.0
            e_b = bg_status._entry(42, sid_b)
            e_b.status = "finished"
            e_b.last_change = 200.0

            out = bg_status.render_panel(42)

            # Two ``  \n`` hard breaks: header→row1 and row1→row2.
            assert out.count("  \n") == 2
            # Both names present.
            assert "alpha" in out
            assert "beta" in out
        finally:
            session_manager.sessions.pop(sid_a, None)
            session_manager.sessions.pop(sid_b, None)

    def test_empty_panel_returns_empty_string(self, isolated_bg) -> None:
        """No bg sessions registered → empty panel, no orphan ``  \\n``."""
        assert bg_status.render_panel(42) == ""


@pytest.mark.asyncio
async def test_infer_status_treats_trailing_user_turn_as_working(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "type": "assistant",
                    "message": {"stop_reason": "end_turn", "content": []},
                },
                {"type": "user", "message": {"content": "next request"}},
            )
        ),
        encoding="utf-8",
    )
    sess = _seed_session("turn-open", "turn-open")
    sess.claude_session_id = "claude-turn-open"
    try:
        from ccbot import session_claude_io

        monkeypatch.setattr(
            session_claude_io,
            "build_session_file_path",
            lambda *_args, **_kwargs: transcript,
        )

        assert await bg_status.infer_status_from_jsonl(sess) == "working"
    finally:
        session_manager.sessions.pop(sess.id, None)


def test_legacy_finished_state_migrates_to_seen(isolated_bg) -> None:
    bg_status.load_per_user(
        {
            "42": {
                "old": {
                    "status": "finished",
                    "last_change": 1.0,
                    "context_pct": None,
                }
            }
        }
    )

    assert bg_status.status_emoji(42, "old") == "☑️"


def test_v2_unread_completion_migrates_to_two_view_flow(isolated_bg) -> None:
    bg_status.load_per_user(
        {
            "42": {
                "unread": {
                    "status": "finished",
                    "status_version": 2,
                    "last_change": 1.0,
                    "context_pct": None,
                }
            }
        }
    )

    assert bg_status.status_emoji(42, "unread") == "✅"
    assert bg_status.record_finished_view(42, "unread") is False
    assert bg_status.record_finished_view(42, "unread") is True
    assert bg_status.status_emoji(42, "unread") == "☑️"


def test_versioned_unread_completion_survives_round_trip(isolated_bg) -> None:
    bg_status.update_status(42, "fresh", "finished")
    assert bg_status.record_finished_view(42, "fresh") is False

    raw = bg_status.serialize_per_user()
    assert raw["42"]["fresh"]["status_version"] == 3
    assert raw["42"]["fresh"]["finished_views"] == 1

    bg_status.load_per_user(raw)
    assert bg_status.status_emoji(42, "fresh") == "✅"
    assert bg_status.record_finished_view(42, "fresh") is True
    assert bg_status.status_emoji(42, "fresh") == "☑️"
