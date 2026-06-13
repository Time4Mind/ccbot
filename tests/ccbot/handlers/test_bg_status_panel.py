"""Regression: ``bg_status.render_panel`` joins rows with a hard line
break (``  \\n``) so the rich-message parser doesn't collapse the panel
header and badge rows onto one line.

Field bug from the 2026-06-13 screenshot at
``.ccbot-inbox/1781374419-…`` — single ``\\n`` is a CommonMark soft
break (= space), so the live card showed
``─── фон ─── ⬛ session-name · context 50%`` on one row instead of the
header on a row above each badge.
"""

from __future__ import annotations

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
