"""Cross-platform startup contract for the persistent tmux client."""

from unittest.mock import patch

from ccbot.tmux_control import TmuxControlClient


def test_linux_starts_tmux_control_mode_without_bsd_script_arguments() -> None:
    client = TmuxControlClient("ccbot")

    with patch("ccbot.tmux_control.sys.platform", "linux"):
        command = client._start_command()

    assert command == (
        "tmux",
        "-C",
        "attach-session",
        "-f",
        "read-only,ignore-size,no-output",
        "-t",
        "ccbot",
    )


def test_macos_keeps_pty_wrapper_for_tmux_control_mode() -> None:
    client = TmuxControlClient("ccbot")

    with patch("ccbot.tmux_control.sys.platform", "darwin"):
        command = client._start_command()

    assert command[:4] == ("/usr/bin/script", "-q", "/dev/null", "tmux")
