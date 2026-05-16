"""Tests for orphan claude/window detection added to prevent the
'two processes resuming the same session_id' failure mode."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ccbot.session_recovery import detect_orphan_windows
from ccbot.tmux_manager import TmuxManager, TmuxWindow


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


class TestKillOrphanClaudeProcesses:
    @pytest.mark.asyncio
    async def test_invalid_session_id_skips_pgrep(self) -> None:
        mgr = TmuxManager()
        with patch("ccbot.tmux_manager.subprocess.run") as mock_run:
            killed = await mgr.kill_orphan_claude_processes("not-a-uuid")
        assert killed == 0
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_pids_returned_is_noop(self) -> None:
        mgr = TmuxManager()
        with (
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=_completed("", returncode=1),
            ),
            patch("ccbot.tmux_manager.os.kill") as mock_kill,
        ):
            killed = await mgr.kill_orphan_claude_processes(
                "550e8400-e29b-41d4-a716-446655440000"
            )
        assert killed == 0
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigterm_each_pid(self) -> None:
        mgr = TmuxManager()
        with (
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=_completed("12345\n67890\n"),
            ),
            patch("ccbot.tmux_manager.os.kill") as mock_kill,
        ):
            killed = await mgr.kill_orphan_claude_processes(
                "550e8400-e29b-41d4-a716-446655440000"
            )
        assert killed == 2
        # Both PIDs got SIGTERM
        signalled = {call.args[0] for call in mock_kill.call_args_list}
        assert signalled == {12345, 67890}

    @pytest.mark.asyncio
    async def test_skip_own_pid_and_parent(self) -> None:
        mgr = TmuxManager()
        own = os.getpid()
        parent = os.getppid()
        # pgrep returns own + parent + one legit pid → only the legit pid
        # gets killed; killing our own ancestors would self-destruct the bot.
        stdout = f"{own}\n{parent}\n99999\n"
        with (
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=_completed(stdout),
            ),
            patch("ccbot.tmux_manager.os.kill") as mock_kill,
        ):
            killed = await mgr.kill_orphan_claude_processes(
                "550e8400-e29b-41d4-a716-446655440000"
            )
        assert killed == 1
        mock_kill.assert_called_once_with(99999, mock_kill.call_args[0][1])

    @pytest.mark.asyncio
    async def test_process_already_gone_is_swallowed(self) -> None:
        mgr = TmuxManager()
        with (
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=_completed("99999\n"),
            ),
            patch("ccbot.tmux_manager.os.kill", side_effect=ProcessLookupError()),
        ):
            killed = await mgr.kill_orphan_claude_processes(
                "550e8400-e29b-41d4-a716-446655440000"
            )
        # Process exited between pgrep and kill — already dead, not a kill.
        assert killed == 0

    @pytest.mark.asyncio
    async def test_pgrep_timeout_is_swallowed(self) -> None:
        mgr = TmuxManager()
        with patch(
            "ccbot.tmux_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5),
        ):
            killed = await mgr.kill_orphan_claude_processes(
                "550e8400-e29b-41d4-a716-446655440000"
            )
        assert killed == 0


def _session(window_id: str) -> SimpleNamespace:
    return SimpleNamespace(window_id=window_id)


def _window(window_id: str, window_name: str) -> TmuxWindow:
    return TmuxWindow(window_id=window_id, window_name=window_name, cwd="/tmp")


class TestDetectOrphanWindows:
    @pytest.mark.asyncio
    async def test_returns_zero_when_all_windows_bound(self) -> None:
        mgr = SimpleNamespace(
            sessions={"s1": _session("@10"), "s2": _session("@11")},
            window_states={},
        )
        windows = [_window("@10", "alpha"), _window("@11", "beta")]
        n = await detect_orphan_windows(mgr, windows=windows)  # type: ignore[arg-type]
        assert n == 0

    @pytest.mark.asyncio
    async def test_reserved_windows_ignored(self) -> None:
        # __main__ and ccbot-usage are bot-owned utility windows; they
        # must never trigger the orphan warning even with no Session.
        mgr = SimpleNamespace(sessions={}, window_states={})
        windows = [
            _window("@0", "__main__"),
            _window("@5", "ccbot-usage"),
        ]
        n = await detect_orphan_windows(mgr, windows=windows)  # type: ignore[arg-type]
        assert n == 0

    @pytest.mark.asyncio
    async def test_unbound_user_window_reported(self) -> None:
        # bounties window exists in tmux but no Session record points at
        # it → exactly the failure mode we're guarding against.
        mgr = SimpleNamespace(
            sessions={"s1": _session("@11")},
            window_states={},
        )
        windows = [_window("@10", "bounties"), _window("@11", "ccbot-2")]
        n = await detect_orphan_windows(mgr, windows=windows)  # type: ignore[arg-type]
        assert n == 1

    @pytest.mark.asyncio
    async def test_pulls_windows_lazily_when_none(self) -> None:
        mgr = SimpleNamespace(sessions={}, window_states={})
        with patch(
            "ccbot.session_recovery.tmux_manager.list_windows",
            return_value=[_window("@10", "ccbot-usage")],
        ) as mock_list:
            n = await detect_orphan_windows(mgr)  # type: ignore[arg-type]
        mock_list.assert_awaited_once()
        assert n == 0
