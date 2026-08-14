from unittest.mock import patch

from ccbot.voice_install import _privileged


def test_privileged_keeps_command_when_running_as_root() -> None:
    command = ["apt-get", "install", "ffmpeg"]
    with patch("ccbot.voice_install.os.geteuid", return_value=0):
        assert _privileged(command) == command


def test_privileged_uses_non_interactive_sudo_for_service_user() -> None:
    command = ["apt-get", "install", "ffmpeg"]
    with (
        patch("ccbot.voice_install.os.geteuid", return_value=1000),
        patch("ccbot.voice_install.shutil.which", return_value="/usr/bin/sudo"),
    ):
        assert _privileged(command) == ["sudo", "-n", *command]


def test_privileged_preserves_existing_failure_when_sudo_is_unavailable() -> None:
    command = ["apt-get", "install", "ffmpeg"]
    with (
        patch("ccbot.voice_install.os.geteuid", return_value=1000),
        patch("ccbot.voice_install.shutil.which", return_value=None),
    ):
        assert _privileged(command) == command
