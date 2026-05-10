"""Tests for SessionManager pure dict operations.

The legacy thread-binding / group_chat_id surface (used by the old
supergroup-forum routing model) was deleted in the DM-only refactor —
those tests live in `doc/legacy/topic-architecture.md` for reference
only.
"""

import pytest

from ccbot.session import SessionManager, key_matches_window


class TestKeyMatchesWindow:
    """``ccbot:@21`` and ``ccbot-w20:@21`` should both resolve to ``@21``.

    The grouped-session prefix sneaks into ``session_map.json`` when a
    Claude hook resolves ``#{session_name}`` from a pane shared via a
    per-window group. The bot must tolerate it; otherwise the session
    is invisible and the user sees the "task ran in tmux but bot is
    silent" symptom.
    """

    def test_canonical_matches(self) -> None:
        assert key_matches_window("ccbot:@21", "@21") is True

    def test_grouped_prefix_matches(self) -> None:
        assert key_matches_window("ccbot-w29:@29", "@29") is True

    def test_mismatched_window_id_rejected(self) -> None:
        assert key_matches_window("ccbot:@21", "@22") is False

    def test_unrelated_session_rejected(self) -> None:
        assert key_matches_window("other-server:@21", "@21") is False

    def test_grouped_with_non_digit_tail_rejected(self) -> None:
        # Only ccbot-w<digits> is a grouped session — anything else
        # could be an unrelated tmux session that happens to start
        # with our name.
        assert key_matches_window("ccbot-wfoo:@21", "@21") is False

    def test_no_colon_rejected(self) -> None:
        assert key_matches_window("ccbot@21", "@21") is False


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
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
        assert mgr.is_window_id("@0") is True
        assert mgr.is_window_id("@12") is True
        assert mgr.is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr.is_window_id("myproject") is False
        assert mgr.is_window_id("@") is False
        assert mgr.is_window_id("") is False
        assert mgr.is_window_id("@abc") is False


class TestLocalTerminalSetting:
    """The setting is 3-state (off / manual / auto); legacy ``on`` migrates."""

    def test_default_is_off(self, mgr: SessionManager) -> None:
        assert mgr.get_user_settings(1).get("local_terminal") == "off"

    def test_explicit_manual_stays(self, mgr: SessionManager) -> None:
        mgr.user_settings[1] = {"local_terminal": "manual"}
        assert mgr.get_user_settings(1).get("local_terminal") == "manual"

    def test_explicit_auto_stays(self, mgr: SessionManager) -> None:
        mgr.user_settings[1] = {"local_terminal": "auto"}
        assert mgr.get_user_settings(1).get("local_terminal") == "auto"

    def test_legacy_on_migrates_to_auto(self, mgr: SessionManager) -> None:
        """Pre-PR state where the setting was binary on/off — the stored
        ``on`` reads back as ``auto`` so old users keep their old auto-
        spawn behavior without re-clicking the settings screen."""
        mgr.user_settings[1] = {"local_terminal": "on"}
        assert mgr.get_user_settings(1).get("local_terminal") == "auto"


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
