"""Resource-bounded tmux discovery used by background polling."""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.tmux_manager import TmuxManager


@pytest.mark.asyncio
async def test_polling_snapshot_reads_all_active_panes_in_one_tmux_call():
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b"@0\t__main__\t/Users/test\tzsh\t1\n"
                b"@3\tone\t/one\tcodex\t1\n"
                b"@4\ttwo\t/two\tpython\t0\n"
                b"@4\ttwo\t/two\tcodex\t1\n",
                b"",
            )
        ),
    )
    manager = TmuxManager(session_name="ccbot")

    with patch(
        "ccbot.tmux_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ) as spawn:
        windows = await manager.polling_snapshot()

    assert [
        (w.window_id, w.window_name, w.cwd, w.pane_current_command) for w in windows
    ] == [
        ("@3", "one", "/one", "codex"),
        ("@4", "two", "/two", "codex"),
    ]
    assert spawn.await_count == 1
    assert spawn.await_args.args[:5] == (
        "tmux",
        "list-panes",
        "-s",
        "-t",
        "ccbot",
    )


@pytest.mark.asyncio
async def test_plain_pane_capture_uses_one_tmux_call():
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"one\ntwo\n", b"")),
    )
    manager = TmuxManager(session_name="ccbot")

    with patch(
        "ccbot.tmux_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ) as spawn:
        captured = await manager.capture_pane("@3")

    assert captured == "one\ntwo\n"
    spawn.assert_awaited_once_with(
        "tmux",
        "capture-pane",
        "-p",
        "-t",
        "@3",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
