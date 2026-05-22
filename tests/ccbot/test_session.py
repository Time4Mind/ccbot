"""Tests for SessionManager pure dict operations.

The legacy thread-binding / group_chat_id surface (used by the old
supergroup-forum routing model) was deleted in the DM-only refactor —
those tests live in `doc/legacy/topic-architecture.md` for reference
only.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.config import config
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


# A pane mid-compaction: spinner line above the input chrome separator.
_BUSY_PANE = "✻ Compacting conversation…\n" + "─" * 26 + "\n❯\n" + "─" * 26
# A settled pane: input chrome only, no spinner line.
_IDLE_PANE = "─" * 26 + "\n❯\n" + "─" * 26


class TestResumeSettleGate:
    """``send_to_window`` holds the first message to a freshly-resumed
    window until the pane stops compacting — typing mid-compaction drops
    the prompt (the reported "instruction not executed after restore" bug).
    """

    @pytest.fixture
    def fast_gate(self, monkeypatch) -> None:
        """Shrink the settle timings so the test runs in ms."""
        monkeypatch.setattr("ccbot.session._RESUME_SETTLE_BUSY_GRACE", 0.1)
        monkeypatch.setattr("ccbot.session._RESUME_SETTLE_IDLE_STABLE", 0.05)
        monkeypatch.setattr("ccbot.session._RESUME_SETTLE_POLL", 0.02)
        monkeypatch.setattr(config, "resume_settle_timeout", 5.0)

    def _mock_tmux(self, monkeypatch, capture_side_effect) -> MagicMock:
        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@1"))
        mock_tmux.capture_pane = AsyncMock(side_effect=capture_side_effect)
        mock_tmux.send_keys = AsyncMock(return_value=True)
        monkeypatch.setattr("ccbot.session.tmux_manager", mock_tmux)
        return mock_tmux

    @pytest.mark.asyncio
    async def test_holds_until_compaction_ends(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        calls = {"n": 0}

        def cap(_wid):
            calls["n"] += 1
            return _BUSY_PANE if calls["n"] <= 2 else _IDLE_PANE

        mock_tmux = self._mock_tmux(monkeypatch, cap)
        mgr.mark_window_resuming("@1")

        ok, _ = await mgr.send_to_window("@1", "do the thing")

        assert ok is True
        # Sent only after the pane went (and stayed) idle.
        mock_tmux.send_keys.assert_awaited_once()
        assert calls["n"] >= 3  # saw busy at least twice, then idle
        assert "@1" not in mgr._resuming_windows  # gate cleared

    @pytest.mark.asyncio
    async def test_small_session_sends_after_grace(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """Never-busy pane → no compaction → send after the busy grace."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: _IDLE_PANE)
        mgr.mark_window_resuming("@1")

        ok, _ = await mgr.send_to_window("@1", "hi")

        assert ok is True
        mock_tmux.send_keys.assert_awaited_once()
        assert mock_tmux.capture_pane.await_count >= 1

    @pytest.mark.asyncio
    async def test_non_resuming_window_sends_immediately(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """A window that wasn't ``--resume``d skips the settle gate."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: _IDLE_PANE)
        # Not marked resuming.
        ok, _ = await mgr.send_to_window("@1", "hi")

        assert ok is True
        mock_tmux.send_keys.assert_awaited_once()
        mock_tmux.capture_pane.assert_not_called()

    def test_mark_noop_when_disabled(self, mgr: SessionManager, monkeypatch) -> None:
        """resume_settle_timeout=0 disables the gate — nothing is flagged."""
        monkeypatch.setattr(config, "resume_settle_timeout", 0.0)
        mgr.mark_window_resuming("@9")
        assert "@9" not in mgr._resuming_windows
