"""Tests for local_terminal — string-building helpers (no subprocess spawn)."""

from ccbot.local_terminal import (
    LINUX_TEMPLATES,
    _build_linux_shell_cmd,
    _build_tmux_command,
    _expand_linux_template,
    _iterm_args,
    _quote_applescript,
    _terminal_app_args,
    group_session_name,
)


class TestQuoteApplescript:
    def test_plain_text(self) -> None:
        assert _quote_applescript("hello") == '"hello"'

    def test_escapes_double_quote(self) -> None:
        assert _quote_applescript('a"b') == '"a\\"b"'

    def test_escapes_backslash(self) -> None:
        assert _quote_applescript("a\\b") == '"a\\\\b"'


class TestGroupSessionName:
    def test_strips_at_prefix(self) -> None:
        assert group_session_name("@5") == "ccbot-w5"

    def test_handles_bare_id(self) -> None:
        assert group_session_name("21") == "ccbot-w21"

    def test_falls_back_for_empty(self) -> None:
        # Defensive fallback so we never produce ``ccbot-w`` with no suffix.
        assert group_session_name("@") == "ccbot-w0"


class TestBuildTmuxCommand:
    def test_uses_per_window_grouped_session(self) -> None:
        """A grouped session — ``new-session -t <source> -s <source>-w<wid>``
        — is what gives each terminal an independent current-window
        view. Without it, ``select-window`` mutates the source's
        current window and drags every other client onto it."""
        cmd = _build_tmux_command("@5")
        assert "new-session -d -t" in cmd
        assert "ccbot-w5" in cmd
        assert "select-window -t" in cmd
        assert "attach-session -t" in cmd

    def test_select_window_targets_group(self) -> None:
        """``select-window`` must target the per-window grouped session
        (``<group>:<wid>``), not the source — otherwise we're back to
        mutating the source's current window."""
        cmd = _build_tmux_command("@7")
        assert "ccbot-w7:@7" in cmd
        # And the older session-level target must NOT be present —
        # that was the bug we are fixing.
        assert "ccbot:@7" not in cmd

    def test_no_switch_client(self) -> None:
        """``switch-client -t <session>:<wid>`` was the bug — it mutates
        the source session's current window for every attached
        client. The fix replaces it with grouped-session attach."""
        cmd = _build_tmux_command("@1")
        assert "switch-client" not in cmd

    def test_keeps_window_open_after_detach(self) -> None:
        """``exec ${SHELL:-bash} -l`` prevents the terminal from snapping
        shut when the user detaches from tmux or attach fails."""
        cmd = _build_tmux_command("@1")
        assert "exec ${SHELL:-bash} -l" in cmd

    def test_wrapped_in_bash_c_for_shell_semantics(self) -> None:
        """Without `bash -c`, iTerm/Terminal.app exec the command without
        a shell — ``;`` loses meaning, the chain falls apart."""
        cmd = _build_tmux_command("@9")
        assert cmd.startswith("bash -c ")

    def test_uses_absolute_tmux_path(self) -> None:
        """iTerm's bash inherits a stripped PATH on Apple Silicon —
        ``tmux`` from Homebrew is invoked by absolute path."""
        cmd = _build_tmux_command("@1")
        # Either an absolute path, OR a bare 'tmux' fallback when
        # shutil.which returned nothing on this host. Reject neither.
        assert "/tmux" in cmd or " tmux " in cmd

    def test_swallows_new_session_failure(self) -> None:
        """``new-session -s <existing>`` errors when the group already
        exists from a previous open of the same window. ``2>/dev/null``
        keeps it quiet so the chain proceeds to attach."""
        cmd = _build_tmux_command("@1")
        assert "new-session -d -t" in cmd
        assert "2>/dev/null" in cmd


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


class TestLinuxTemplates:
    def test_known_emulators_present(self) -> None:
        for name in ("gnome-terminal", "kitty", "wezterm", "alacritty", "xterm"):
            assert name in LINUX_TEMPLATES
            assert "{shell}" in LINUX_TEMPLATES[name]

    def test_build_linux_shell_cmd_uses_grouped_session(self) -> None:
        """Linux path mirrors macOS: per-window grouped session so
        ``select-window`` does not steal other already-attached clients."""
        cmd = _build_linux_shell_cmd("@7")
        assert "new-session -d -t" in cmd
        assert "ccbot-w7" in cmd
        assert "select-window -t" in cmd
        assert "attach-session -t" in cmd
        assert "exec bash -i" in cmd

    def test_expand_linux_template_returns_argv(self) -> None:
        argv = _expand_linux_template("gnome-terminal -- bash -c {shell}", "@5")
        assert argv[0] == "gnome-terminal"
        assert argv[1] == "--"
        assert argv[2] == "bash"
        assert argv[3] == "-c"
        # Last element is the quoted shell snippet.
        assert "ccbot-w5" in argv[4]

    def test_expand_linux_template_quotes_special_chars(self) -> None:
        argv = _expand_linux_template("kitty bash -c {shell}", "@1")
        # The shell snippet is delivered as a single argv element — the
        # emulator should not see ``;`` split into multiple args.
        assert len(argv) == 4
        assert "new-session -d -t" in argv[3]
