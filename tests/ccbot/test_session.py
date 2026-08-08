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
from ccbot.session_models import Session


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


class TestIdleArchiveSetting:
    def test_default_is_six_hours(self, mgr: SessionManager) -> None:
        assert mgr.get_user_settings(1).get("session_idle_hours") == 6

    def test_explicit_value_is_persisted(self, mgr: SessionManager) -> None:
        mgr.update_user_setting(1, "session_idle_hours", 12)
        assert mgr.get_user_settings(1).get("session_idle_hours") == 12


class TestGlobalAgentBackend:
    def test_switch_persists_bot_wide_value(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "agent_backend", "claude")
        save = MagicMock()
        monkeypatch.setattr(mgr, "save_state", save)
        mgr.agent_backend = "claude"

        mgr.set_agent_backend("codex")

        assert mgr.agent_backend == "codex"
        assert config.agent_backend == "codex"
        save.assert_called_once_with()

    def test_switch_rejected_while_old_backend_is_live(
        self, mgr: SessionManager
    ) -> None:
        mgr.agent_backend = "claude"
        mgr.sessions["live"] = Session(
            id="live",
            name="work",
            state="idle",
            backend="claude",
        )

        with pytest.raises(RuntimeError, match="archive live sessions"):
            mgr.set_agent_backend("codex")

        assert mgr.agent_backend == "claude"

    def test_archived_old_backend_does_not_block_switch(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "agent_backend", "claude")
        mgr.agent_backend = "claude"
        mgr.sessions["old"] = Session(
            id="old",
            name="history",
            state="archived",
            backend="claude",
        )

        mgr.set_agent_backend("codex")

        assert mgr.agent_backend == "codex"


class TestActiveSessions:
    def test_no_active_session_initially(self, mgr: SessionManager) -> None:
        assert mgr.get_active_session(100) is None
        assert mgr.get_active_window(100) is None

    # NOTE: the create→set_active→get_active_window→clear round-trip lives in
    # test_session_dm.py::TestActiveSessions (test_create_then_active +
    # test_delete_session). Kept here only the empty-initial-state guard above.


# A pane mid-compaction: spinner line above the input chrome separator.
_BUSY_PANE = "✻ Compacting conversation…\n" + "─" * 26 + "\n❯\n" + "─" * 26
# A settled pane: input chrome only, no spinner line.
_IDLE_PANE = "─" * 26 + "\n❯\n" + "─" * 26
_CODEX_STARTING_PANE = "Starting Codex…"
_CODEX_READY_PANE = "OpenAI Codex\n\n› Write tests for @filename"


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
        mock_tmux.ensure_codex_prompt_submitted = AsyncMock(return_value=True)
        monkeypatch.setattr("ccbot.session.tmux_manager", mock_tmux)
        return mock_tmux

    @pytest.mark.asyncio
    async def test_fresh_codex_start_queues_until_real_prompt(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """A message sent immediately after window creation is held until
        Codex has drawn its input box, then submitted exactly once."""
        panes = iter([_CODEX_STARTING_PANE, _CODEX_READY_PANE])
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: next(panes))
        mgr.mark_window_starting("@1", backend="codex", resume=False)

        ok, message = await mgr.send_to_window("@1", "fix startup")

        assert ok is True
        assert message.startswith("Queued for ")
        mock_tmux.send_keys.assert_not_awaited()
        task = mgr._resume_settle_tasks["@1"]
        await task
        mock_tmux.send_keys.assert_awaited_once_with("@1", "fix startup")
        mock_tmux.ensure_codex_prompt_submitted.assert_awaited_once_with(
            "@1", "fix startup"
        )

    @pytest.mark.asyncio
    async def test_message_arriving_during_drain_is_not_lost(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """The gate keeps draining batches that arrive while a prior queued
        prompt is being submitted."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: _CODEX_READY_PANE)
        second_queued = False

        async def submit(_wid: str, text: str) -> bool:
            nonlocal second_queued
            if text == "first" and not second_queued:
                second_queued = True
                ok, _ = await mgr.send_to_window("@1", "second")
                assert ok is True
            return True

        mock_tmux.ensure_codex_prompt_submitted.side_effect = submit
        mgr.mark_window_starting("@1", backend="codex", resume=False)
        ok, _ = await mgr.send_to_window("@1", "first")
        assert ok is True

        await mgr._resume_settle_tasks["@1"]

        sent = [call.args[1] for call in mock_tmux.send_keys.await_args_list]
        assert sent == ["first", "second"]

    @pytest.mark.parametrize(
        "pane",
        [
            "Do you trust the contents of this directory?\n› 1. Yes, continue",
            "Choose working directory to resume this session\n› 1. Use session directory",
            "Sign in with ChatGPT\n› 1. Continue",
        ],
    )
    def test_codex_startup_screens_are_not_ready(self, pane: str) -> None:
        assert SessionManager._pane_has_ready_input(pane, "codex") is False

    @pytest.mark.asyncio
    async def test_holds_until_compaction_ends(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """Prompt arriving mid-resume is buffered, then drained once the
        pane settles. ``send_to_window`` returns immediately (non-blocking)
        and the background watcher calls ``send_keys`` after settle."""
        calls = {"n": 0}

        def cap(_wid):
            calls["n"] += 1
            return _BUSY_PANE if calls["n"] <= 2 else _IDLE_PANE

        mock_tmux = self._mock_tmux(monkeypatch, cap)
        mgr.mark_window_resuming("@1")

        ok, _ = await mgr.send_to_window("@1", "do the thing")
        # Non-blocking: returns success with the prompt buffered, NOT
        # typed yet — the wait happens in the background watcher.
        assert ok is True
        assert mock_tmux.send_keys.await_count == 0
        assert mgr._pending_sends.get("@1") == ["do the thing"]

        # Wait for the watcher to finish (it polls every 20ms in fast_gate).
        task = mgr._resume_settle_tasks.get("@1")
        assert task is not None
        await task

        mock_tmux.send_keys.assert_awaited_once()
        assert calls["n"] >= 3  # saw busy at least twice, then idle
        assert "@1" not in mgr._resuming_windows  # gate cleared
        assert "@1" not in mgr._pending_sends  # buffer drained

    @pytest.mark.asyncio
    async def test_small_session_sends_after_grace(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """Never-busy pane → no compaction → buffer drains after busy-grace."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: _IDLE_PANE)
        mgr.mark_window_resuming("@1")

        ok, _ = await mgr.send_to_window("@1", "hi")
        assert ok is True
        # Still buffered until the watcher hits busy-grace timeout.
        assert mock_tmux.send_keys.await_count == 0

        task = mgr._resume_settle_tasks.get("@1")
        assert task is not None
        await task

        mock_tmux.send_keys.assert_awaited_once()
        assert mock_tmux.capture_pane.await_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_prompts_buffered_and_drained_in_order(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """Several prompts during a resume should drain in arrival order
        once the pane settles — not get sent concurrently mid-compact."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: _IDLE_PANE)
        mgr.mark_window_resuming("@1")

        for text in ("first", "second", "third"):
            ok, _ = await mgr.send_to_window("@1", text)
            assert ok is True

        # All three are buffered, nothing typed yet.
        assert mock_tmux.send_keys.await_count == 0
        assert mgr._pending_sends["@1"] == ["first", "second", "third"]

        task = mgr._resume_settle_tasks.get("@1")
        assert task is not None
        await task

        assert mock_tmux.send_keys.await_count == 3
        sent_texts = [c.args[1] for c in mock_tmux.send_keys.await_args_list]
        assert sent_texts == ["first", "second", "third"]

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

    @pytest.mark.asyncio
    async def test_watcher_stops_when_window_disappears(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """A killed startup window must not leave a retrying watcher behind."""
        mock_tmux = self._mock_tmux(monkeypatch, lambda _w: None)
        mock_tmux.find_window_by_id.return_value = None
        mgr.mark_window_starting("@1", backend="claude", resume=False)

        task = mgr._resume_settle_tasks["@1"]
        await task

        assert "@1" not in mgr._resuming_windows
        assert "@1" not in mgr._resume_settle_tasks
        assert mock_tmux.capture_pane.await_count == 1
        mock_tmux.send_keys.assert_not_awaited()

    def test_mark_noop_when_disabled(self, mgr: SessionManager, monkeypatch) -> None:
        """resume_settle_timeout=0 disables the gate — nothing is flagged."""
        monkeypatch.setattr(config, "resume_settle_timeout", 0.0)
        mgr.mark_window_resuming("@9")
        assert "@9" not in mgr._resuming_windows
        assert "@9" not in mgr._resume_settle_tasks

    @pytest.mark.asyncio
    async def test_mark_resuming_is_idempotent(
        self, mgr: SessionManager, monkeypatch, fast_gate
    ) -> None:
        """Calling ``mark_window_resuming`` twice for the same window doesn't
        spawn a second watcher (the first claim wins)."""
        self._mock_tmux(monkeypatch, lambda _w: _IDLE_PANE)
        mgr.mark_window_resuming("@1")
        task1 = mgr._resume_settle_tasks.get("@1")
        mgr.mark_window_resuming("@1")
        task2 = mgr._resume_settle_tasks.get("@1")
        assert task1 is task2
        # Cleanup so we don't leave a dangling task across tests.
        if task1:
            await task1
