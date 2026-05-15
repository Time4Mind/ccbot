"""Tests for the DM-mode active_sessions / Session dataclass surface."""

import pytest

from ccbot.session import Session, SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    return SessionManager()


class TestSessionDataclass:
    def test_new_id_is_8_hex_chars(self) -> None:
        sid = Session.new_id()
        assert len(sid) == 8
        assert all(c in "0123456789abcdef" for c in sid)

    def test_round_trip_to_dict(self) -> None:
        s = Session(
            id="abcdef12",
            name="frontend",
            window_id="@5",
            workdir="/tmp/x",
            goal="ship login",
            state="active",
            claude_session_id="uuid-1",
            created_at=100.0,
            last_event_at=200.0,
            archived_at=0.0,
            message_count=5,
        )
        d = s.to_dict()
        s2 = Session.from_dict(d)
        assert s2 == s

    def test_from_dict_clamps_unknown_state(self) -> None:
        s = Session.from_dict({"id": "x", "state": "wat"})
        assert s.state == "active"


class TestActiveSessions:
    def test_create_then_active(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="frontend", window_id="@1", workdir="/tmp")
        assert sess.id in mgr.sessions
        mgr.set_active_session(100, sess.id)
        assert mgr.get_active_session(100) is sess
        assert mgr.get_active_window(100) == "@1"

    def test_set_active_unknown_id_raises(self, mgr: SessionManager) -> None:
        with pytest.raises(KeyError):
            mgr.set_active_session(100, "nonexistent")

    def test_archive_clears_active_pointer(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="x", window_id="@1", workdir="/tmp")
        mgr.set_active_session(100, sess.id)
        mgr.mark_session_archived(sess.id)
        assert mgr.get_active_session(100) is None
        assert mgr.sessions[sess.id].state == "archived"
        assert mgr.sessions[sess.id].window_id == ""

    def test_done_marks_completed(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="x", window_id="@1", workdir="/tmp")
        mgr.mark_session_archived(sess.id, completed=True)
        assert mgr.sessions[sess.id].state == "completed"

    def test_find_idle_to_archive(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="x", window_id="@1", workdir="/tmp")
        # Both anchors must be far in the past — the helper uses
        # last_event_at OR created_at as the anchor.
        sess.last_event_at = 1.0
        sess.created_at = 1.0
        idle = mgr.find_idle_to_archive(idle_seconds=10.0)
        assert sess in idle

    def test_delete_session(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="x", window_id="@1", workdir="/tmp")
        mgr.set_active_session(100, sess.id)
        assert mgr.delete_session(sess.id)
        assert sess.id not in mgr.sessions
        assert mgr.get_active_session(100) is None

    def test_list_archived_filters_age(self, mgr: SessionManager) -> None:
        sess1 = mgr.create_session(name="a", window_id="@1", workdir="/tmp")
        sess2 = mgr.create_session(name="b", window_id="@2", workdir="/tmp")
        mgr.mark_session_archived(sess1.id)
        mgr.mark_session_archived(sess2.id)
        # Force sess1 archived_at into the distant past.
        mgr.sessions[sess1.id].archived_at = 1.0
        recent = mgr.list_archived(max_age_seconds=60.0)
        ids = {s.id for s in recent}
        assert sess2.id in ids
        assert sess1.id not in ids
