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
                b"@0\t__main__\t/Users/test\tzsh\t1\t100\n"
                b"@3\tone\t/one\tcodex\t1\t101\n"
                b"@4\ttwo\t/two\tpython\t0\t102\n"
                b"@4\ttwo\t/two\tcodex\t1\t103\n",
                b"",
            )
        ),
    )
    manager = TmuxManager(session_name="ccbot")

    with (
        patch.object(manager, "_control_request", new=AsyncMock(return_value=None)),
        patch(
            "ccbot.tmux_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as spawn,
    ):
        windows = await manager.polling_snapshot()

    assert [
        (w.window_id, w.window_name, w.cwd, w.pane_current_command) for w in windows
    ] == [
        ("@3", "one", "/one", "codex"),
        ("@4", "two", "/two", "codex"),
    ]
    assert [w.activity for w in windows] == [101, 103]
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


@pytest.mark.asyncio
async def test_batch_pane_capture_reads_multiple_windows_in_one_tmux_call():
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                b"__CCBOT_CAPTURE_TEST__@3\none\ntwo\n"
                b"__CCBOT_CAPTURE_TEST__@4\nthree\n",
                b"",
            )
        ),
    )
    manager = TmuxManager(session_name="ccbot")

    with (
        patch.object(manager, "_control_request", new=AsyncMock(return_value=None)),
        patch(
            "ccbot.tmux_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as spawn,
        patch("ccbot.tmux_manager.secrets.token_hex", return_value="TEST"),
    ):
        captured = await manager.capture_panes(["@3", "@4"], with_ansi=True)

    assert captured == {"@3": "one\ntwo\n", "@4": "three\n"}
    assert spawn.await_count == 1
    assert spawn.await_args.args.count("capture-pane") == 2
    assert spawn.await_args.args.count("-e") == 2


@pytest.mark.asyncio
async def test_polling_snapshot_reuses_persistent_control_client():
    manager = TmuxManager(session_name="ccbot")
    control = AsyncMock(return_value="@3\tone\t/one\tcodex\t1\t101\n")

    with (
        patch.object(manager, "_control_request", new=control),
        patch(
            "ccbot.tmux_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as spawn,
    ):
        windows = await manager.polling_snapshot()

    assert [(window.window_id, window.activity) for window in windows] == [("@3", 101)]
    control.assert_awaited_once()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_capture_reuses_persistent_control_client():
    manager = TmuxManager(session_name="ccbot")
    control = AsyncMock(side_effect=["one\n", "two\n"])

    with (
        patch.object(manager, "_control_request", new=control),
        patch("ccbot.tmux_manager.secrets.token_hex", return_value="TEST"),
        patch(
            "ccbot.tmux_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as spawn,
    ):
        captured = await manager.capture_panes(["@3", "@4"], with_ansi=True)

    assert captured == {"@3": "one\n", "@4": "two\n"}
    assert control.await_count == 2
    assert [call.args[0] for call in control.await_args_list] == [
        "capture-pane -e -p -t @3",
        "capture-pane -e -p -t @4",
    ]
    spawn.assert_not_awaited()
