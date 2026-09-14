"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.monitor_state import MonitorState, TrackedSession
from ccbot.session_monitor import NewMessage, SessionInfo, SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestDirectSessionTargets:
    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_loads_current_server_transcript_paths_from_session_map(
        self, monitor, tmp_path, monkeypatch
    ):
        from ccbot.config import config

        transcript = tmp_path / "active.jsonl"
        transcript.write_text("", encoding="utf-8")
        session_map = tmp_path / "session_map.json"
        session_map.write_text(
            json.dumps(
                {
                    "ccbot:@7": {
                        "session_id": "active-session",
                        "transcript_path": str(transcript),
                    },
                    "other:@8": {
                        "session_id": "foreign-session",
                        "transcript_path": str(tmp_path / "foreign.jsonl"),
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "session_map_file", session_map)
        monkeypatch.setattr(config, "tmux_session_name", "ccbot")

        current_map, targets = await monitor._load_current_session_targets()

        assert current_map == {"@7": "active-session"}
        assert targets == [SessionInfo("active-session", transcript)]

    @pytest.mark.asyncio
    async def test_complete_direct_targets_skip_project_and_tmux_scan(
        self, monitor, tmp_path
    ):
        transcript = tmp_path / "active.jsonl"
        transcript.write_text("", encoding="utf-8")
        monitor.scan_projects = AsyncMock(return_value=[])

        await monitor.check_for_updates(
            {"active-session"},
            session_infos=[SessionInfo("active-session", transcript)],
        )

        monitor.scan_projects.assert_not_awaited()
        assert monitor.state.get_session("active-session") is not None

    def test_uses_only_live_bot_sessions_for_hot_path_targets(
        self, monitor, tmp_path, monkeypatch
    ):
        from ccbot.session import session_manager

        active_path = tmp_path / "active.jsonl"
        monkeypatch.setattr(
            session_manager,
            "sessions",
            {
                "active": SimpleNamespace(
                    state="active",
                    window_id="@7",
                    claude_session_id="active-provider",
                ),
                "archived": SimpleNamespace(
                    state="archived",
                    window_id="@8",
                    claude_session_id="archived-provider",
                ),
            },
        )
        monkeypatch.setattr(
            session_manager,
            "window_states",
            {
                "@7": SimpleNamespace(transcript_path=str(active_path)),
                "@8": SimpleNamespace(transcript_path=str(tmp_path / "old.jsonl")),
            },
        )

        current_map, targets = monitor._current_session_targets()

        assert current_map == {"@7": "active-provider"}
        assert targets == [SessionInfo("active-provider", active_path)]

    @pytest.mark.asyncio
    async def test_missing_direct_target_uses_legacy_scan_fallback(
        self, monitor, tmp_path
    ):
        direct = tmp_path / "direct.jsonl"
        legacy = tmp_path / "legacy.jsonl"
        direct.write_text("", encoding="utf-8")
        legacy.write_text("", encoding="utf-8")
        monitor.scan_projects = AsyncMock(
            return_value=[SessionInfo("legacy-session", legacy)]
        )

        await monitor.check_for_updates(
            {"direct-session", "legacy-session"},
            session_infos=[SessionInfo("direct-session", direct)],
        )

        monitor.scan_projects.assert_awaited_once()
        assert monitor.state.get_session("direct-session") is not None
        assert monitor.state.get_session("legacy-session") is not None


class TestMonitorCheckpointOrdering:
    @staticmethod
    def _terminal_message() -> NewMessage:
        return NewMessage(
            session_id="s1",
            text="done",
            is_complete=True,
            content_type="text",
            role="assistant",
            stop_reason="end_turn",
        )

    @pytest.mark.asyncio
    async def test_terminal_offset_is_saved_after_callback(self, tmp_path, monkeypatch):
        from ccbot.session import session_manager

        state_file = tmp_path / "monitor_state.json"
        monitor = SessionMonitor(state_file=state_file, poll_interval=0.0)
        tracked = TrackedSession("s1", "/tmp/s1.jsonl", 10)
        monitor.state.update_session(tracked)
        monitor.state.save()

        async def check_for_updates(*_args, **_kwargs):
            tracked.last_byte_offset = 20
            monitor.state.update_session(tracked)
            return [self._terminal_message()]

        observed_offsets: list[int] = []

        async def callback(_msg):
            persisted = MonitorState(state_file=state_file)
            persisted.load()
            observed_offsets.append(persisted.tracked_sessions["s1"].last_byte_offset)
            monitor._running = False

        monitor.check_for_updates = check_for_updates
        monitor.set_message_callback(callback)
        monitor._cleanup_all_stale_sessions = AsyncMock()
        monitor._load_current_session_map = AsyncMock(return_value={"@1": "s1"})
        monitor._current_session_targets = lambda: ({"@1": "s1"}, [])
        monitor._detect_and_cleanup_changes = AsyncMock(return_value={"@1": "s1"})
        monkeypatch.setattr(session_manager, "load_session_map", AsyncMock())
        monkeypatch.setattr("ccbot.session_monitor.asyncio.sleep", AsyncMock())

        monitor._running = True
        await monitor._monitor_loop()

        persisted = MonitorState(state_file=state_file)
        persisted.load()
        assert observed_offsets == [10]
        assert persisted.tracked_sessions["s1"].last_byte_offset == 20

    @pytest.mark.asyncio
    async def test_callback_failure_restores_last_persisted_offset(
        self, tmp_path, monkeypatch
    ):
        from ccbot.session import session_manager

        state_file = tmp_path / "monitor_state.json"
        monitor = SessionMonitor(state_file=state_file, poll_interval=0.0)
        tracked = TrackedSession("s1", "/tmp/s1.jsonl", 10)
        monitor.state.update_session(tracked)
        monitor.state.save()

        async def check_for_updates(*_args, **_kwargs):
            tracked.last_byte_offset = 20
            monitor.state.update_session(tracked)
            return [self._terminal_message()]

        async def callback(_msg):
            monitor._running = False
            raise RuntimeError("delivery failed")

        monitor.check_for_updates = check_for_updates
        monitor.set_message_callback(callback)
        monitor._cleanup_all_stale_sessions = AsyncMock()
        monitor._load_current_session_map = AsyncMock(return_value={"@1": "s1"})
        monitor._current_session_targets = lambda: ({"@1": "s1"}, [])
        monitor._detect_and_cleanup_changes = AsyncMock(return_value={"@1": "s1"})
        monkeypatch.setattr(session_manager, "load_session_map", AsyncMock())
        monkeypatch.setattr("ccbot.session_monitor.asyncio.sleep", AsyncMock())

        monitor._running = True
        await monitor._monitor_loop()

        assert monitor.state.tracked_sessions["s1"].last_byte_offset == 10
        persisted = MonitorState(state_file=state_file)
        persisted.load()
        assert persisted.tracked_sessions["s1"].last_byte_offset == 10

    @pytest.mark.asyncio
    async def test_poll_failure_restores_last_persisted_offset(
        self, tmp_path, monkeypatch
    ):
        from ccbot.session import session_manager

        state_file = tmp_path / "monitor_state.json"
        monitor = SessionMonitor(state_file=state_file, poll_interval=0.0)
        tracked = TrackedSession("s1", "/tmp/s1.jsonl", 10)
        monitor.state.update_session(tracked)
        monitor.state.save()

        async def check_for_updates(*_args, **_kwargs):
            tracked.last_byte_offset = 20
            monitor.state.update_session(tracked)
            monitor._running = False
            raise RuntimeError("parse failed")

        monitor.check_for_updates = check_for_updates
        monitor._cleanup_all_stale_sessions = AsyncMock()
        monitor._load_current_session_map = AsyncMock(return_value={"@1": "s1"})
        monitor._current_session_targets = lambda: ({"@1": "s1"}, [])
        monitor._detect_and_cleanup_changes = AsyncMock(return_value={"@1": "s1"})
        monkeypatch.setattr(session_manager, "load_session_map", AsyncMock())
        monkeypatch.setattr("ccbot.session_monitor.asyncio.sleep", AsyncMock())

        monitor._running = True
        await monitor._monitor_loop()

        assert monitor.state.tracked_sessions["s1"].last_byte_offset == 10

    @pytest.mark.asyncio
    async def test_failure_does_not_replay_another_sessions_delivered_terminal(
        self, tmp_path, monkeypatch
    ):
        from ccbot.session import session_manager

        state_file = tmp_path / "monitor_state.json"
        monitor = SessionMonitor(state_file=state_file, poll_interval=0.0)
        first = TrackedSession("s1", "/tmp/s1.jsonl", 10)
        second = TrackedSession("s2", "/tmp/s2.jsonl", 10)
        monitor.state.update_session(first)
        monitor.state.update_session(second)
        monitor.state.save()

        async def check_for_updates(*_args, **_kwargs):
            first.last_byte_offset = 20
            second.last_byte_offset = 20
            monitor.state.update_session(first)
            monitor.state.update_session(second)
            return [
                self._terminal_message(),
                NewMessage(
                    session_id="s2",
                    text="done two",
                    is_complete=True,
                    content_type="text",
                    role="assistant",
                    stop_reason="end_turn",
                ),
            ]

        delivered: list[str] = []

        async def callback(msg):
            if msg.session_id == "s2":
                monitor._running = False
                raise RuntimeError("delivery failed")
            delivered.append(msg.session_id)

        monitor.check_for_updates = check_for_updates
        monitor.set_message_callback(callback)
        monitor._cleanup_all_stale_sessions = AsyncMock()
        monitor._load_current_session_map = AsyncMock(
            return_value={"@1": "s1", "@2": "s2"}
        )
        monitor._current_session_targets = lambda: (
            {"@1": "s1", "@2": "s2"},
            [],
        )
        monitor._detect_and_cleanup_changes = AsyncMock(
            return_value={"@1": "s1", "@2": "s2"}
        )
        monkeypatch.setattr(session_manager, "load_session_map", AsyncMock())
        monkeypatch.setattr("ccbot.session_monitor.asyncio.sleep", AsyncMock())

        monitor._running = True
        await monitor._monitor_loop()

        persisted = MonitorState(state_file=state_file)
        persisted.load()
        assert delivered == ["s1"]
        assert persisted.tracked_sessions["s1"].last_byte_offset == 20
        assert persisted.tracked_sessions["s2"].last_byte_offset == 10
