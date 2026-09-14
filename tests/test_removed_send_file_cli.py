"""The retired filesystem send-file relay must not remain callable."""

from __future__ import annotations

import sys

import pytest

from ccbot.main import main


def test_send_file_command_is_explicitly_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["ccbot", "send-file", "/tmp/report.txt"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "ccbot send-file has been removed" in capsys.readouterr().err
