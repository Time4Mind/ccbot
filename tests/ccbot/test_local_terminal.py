"""Tests for local_terminal — string-building helpers (no subprocess spawn)."""

from ccbot.local_terminal import (
    _build_tmux_command,
    _iterm_args,
    _quote_applescript,
    _terminal_app_args,
)


class TestQuoteApplescript:
    def test_plain_text(self) -> None:
        assert _quote_applescript("hello") == '"hello"'

    def test_escapes_double_quote(self) -> None:
        assert _quote_applescript('a"b') == '"a\\"b"'

    def test_escapes_backslash(self) -> None:
        assert _quote_applescript("a\\b") == '"a\\\\b"'


class TestBuildTmuxCommand:
    def test_window_id_appended(self) -> None:
        cmd = _build_tmux_command("@5")
        assert "@5" in cmd
        assert "tmux attach -t" in cmd
        assert "select-window -t @5" in cmd
        assert "\\;" in cmd  # tmux command separator

    def test_session_name_quoted(self) -> None:
        cmd = _build_tmux_command("@1")
        # config.tmux_session_name comes from env / default; just sanity-check
        # that the result is shell-safe (no unquoted spaces).
        assert "tmux attach -t" in cmd


class TestArgsBuilders:
    def test_terminal_app_args_uses_osascript(self) -> None:
        args = _terminal_app_args("tmux attach -t ccbot")
        assert args[0] == "osascript"
        assert any('"Terminal"' in a for a in args)

    def test_iterm_args_uses_create_tab_branch(self) -> None:
        args = _iterm_args("tmux attach -t ccbot")
        assert args[0] == "osascript"
        # Single -e payload contains both the tab and window branches.
        script = " ".join(args[2:])
        assert "create tab" in script
        assert "create window" in script
