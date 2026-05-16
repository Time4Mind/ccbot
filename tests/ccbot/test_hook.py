"""Tests for Claude Code session tracking hook."""

import io
import json
import sys
from typing import Any

import pytest

from ccbot.hook import _UUID_RE, _is_hook_installed, hook_main


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


def _hook_entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command, "timeout": 5}]}


def _both_events(command: str) -> dict[str, Any]:
    return {
        "hooks": {
            "SessionStart": [_hook_entry(command)],
            "UserPromptSubmit": [_hook_entry(command)],
        }
    }


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        assert _is_hook_installed(_both_events("ccbot hook")) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings: dict[str, Any] = {
            "hooks": {
                "SessionStart": [_hook_entry("other-tool hook")],
                "UserPromptSubmit": [_hook_entry("other-tool hook")],
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        assert _is_hook_installed(_both_events("/usr/bin/ccbot hook")) is True

    def test_partial_install_returns_false(self) -> None:
        # SessionStart registered but UserPromptSubmit missing → install
        # is incomplete and must be re-run to wire the second event.
        settings: dict[str, Any] = {
            "hooks": {"SessionStart": [_hook_entry("ccbot hook")]}
        }
        assert _is_hook_installed(settings) is False


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_unsupported_event_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Stop / PreToolUse / etc. are not in _HOOK_EVENTS — must be no-op.
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()
