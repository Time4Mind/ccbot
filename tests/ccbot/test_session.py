"""Tests for SessionManager pure dict operations.

The legacy thread-binding / group_chat_id surface (used by the old
supergroup-forum routing model) was deleted in the DM-only refactor —
those tests live in `doc/legacy/topic-architecture.md` for reference
only.
"""

import pytest

from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_update_display_name(self, mgr: SessionManager) -> None:
        mgr.update_display_name("@1", "myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_update_display_name_overwrites(self, mgr: SessionManager) -> None:
        mgr.update_display_name("@1", "old-name")
        mgr.update_display_name("@1", "new-name")
        assert mgr.get_display_name("@1") == "new-name"


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False


class TestActiveSessions:
    def test_no_active_session_initially(self, mgr: SessionManager) -> None:
        assert mgr.get_active_session(100) is None
        assert mgr.get_active_window(100) is None

    def test_set_and_clear_active_session(self, mgr: SessionManager) -> None:
        sess = mgr.create_session(name="x", window_id="@1", workdir="/tmp")
        mgr.set_active_session(100, sess.id)
        assert mgr.get_active_session(100) is not None
        assert mgr.get_active_session(100).id == sess.id  # type: ignore[union-attr]
        assert mgr.get_active_window(100) == "@1"
        mgr.clear_active_session(100)
        assert mgr.get_active_session(100) is None
