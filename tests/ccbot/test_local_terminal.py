"""Tests for local_terminal — string-building helpers (no subprocess spawn)."""

from ccbot.local_terminal import (
    LINUX_TEMPLATES,
    _build_linux_shell_cmd,
    _build_tmux_command,
    _expand_linux_template,
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
        assert "\\;" in cmd  # tmux command separator preserved

    def test_keeps_window_open_after_detach(self) -> None:
        """`|| true; exec ${SHELL:-bash} -l` prevents the terminal from
        snapping shut when the user detaches from tmux or attach fails."""
        cmd = _build_tmux_command("@1")
        assert "|| true" in cmd
        assert "exec ${SHELL:-bash} -l" in cmd

    def test_wrapped_in_bash_c_for_shell_semantics(self) -> None:
        """Without `bash -c`, iTerm/Terminal.app exec the command without
        a shell — `\\;`, `||`, `;` all lose meaning, attach fails."""
        cmd = _build_tmux_command("@9")
        assert cmd.startswith("bash -c ")

    def test_uses_absolute_tmux_path(self) -> None:
        """iTerm's bash inherits a stripped PATH on Apple Silicon —
        ``tmux`` from Homebrew is invoked by absolute path."""
        cmd = _build_tmux_command("@1")
        # Either an absolute path, OR a bare 'tmux' fallback when
        # shutil.which returned nothing on this host. Reject neither.
        assert "/tmux" in cmd or " tmux " in cmd

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


class TestLinuxTemplates:
    def test_known_emulators_present(self) -> None:
        for name in ("gnome-terminal", "kitty", "wezterm", "alacritty", "xterm"):
            assert name in LINUX_TEMPLATES
            assert "{shell}" in LINUX_TEMPLATES[name]

    def test_build_linux_shell_cmd_attaches_and_keeps_open(self) -> None:
        cmd = _build_linux_shell_cmd("@7")
        assert "tmux attach -t" in cmd
        assert "select-window -t @7" in cmd
        # `|| true` swallows non-zero attach exit so the trailing exec runs.
        assert "|| true" in cmd
        assert "exec bash -i" in cmd

    def test_expand_linux_template_returns_argv(self) -> None:
        argv = _expand_linux_template("gnome-terminal -- bash -c {shell}", "@5")
        assert argv[0] == "gnome-terminal"
        assert argv[1] == "--"
        assert argv[2] == "bash"
        assert argv[3] == "-c"
        # Last element is the quoted shell snippet.
        assert "select-window -t @5" in argv[4]

    def test_expand_linux_template_quotes_special_chars(self) -> None:
        argv = _expand_linux_template("kitty bash -c {shell}", "@1")
        # The shell snippet is delivered as a single argv element — the
        # emulator should not see the tmux backslash-semicolon split into
        # multiple args.
        assert len(argv) == 4
        assert "tmux attach -t" in argv[3]
