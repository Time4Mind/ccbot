"""Literal terminal input is pasted in bounded UTF-8-safe chunks."""

import asyncio
import os

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccbot import tmux_input_transport
from ccbot.tmux_manager import TmuxManager


def test_terminal_input_chunks_preserve_utf8_and_byte_limit() -> None:
    text = "абв🙂" * 400
    chunks = tmux_input_transport.terminal_input_chunks(text, backend="claude")
    assert b"".join(chunks).decode() == text
    assert all(len(chunk) <= 900 for chunk in chunks)
    assert len(chunks) > 1


def test_codex_transport_adds_only_trailing_space() -> None:
    text = "проверь @README"
    chunks = tmux_input_transport.terminal_input_chunks(text, backend="codex")
    assert b"".join(chunks).decode() == text + " "


@pytest.mark.asyncio
async def test_tmux_command_drops_inherited_client_identity(monkeypatch) -> None:
    """A bot launched inside a read-only client must use a fresh command client."""
    monkeypatch.setenv("TMUX", "/run/tmux-1000/default,123,7")
    monkeypatch.setenv("TMUX_PANE", "%42")
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"", b""))
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    assert await tmux_input_transport._run_tmux("send-keys", "-t", "@1", "C-m") == (
        0,
        b"",
    )

    argv = create.await_args.args
    kwargs = create.await_args.kwargs
    assert argv[:3] == ("tmux", "-S", "/run/tmux-1000/default")
    assert argv[3:] == ("send-keys", "-t", "@1", "C-m")
    assert "TMUX" not in kwargs["env"]
    assert "TMUX_PANE" not in kwargs["env"]
    assert kwargs["env"]["PATH"] == os.environ["PATH"]


@pytest.mark.asyncio
async def test_literal_input_pastes_chunks_in_order_then_one_carriage_return() -> None:
    manager = TmuxManager(session_name="ccbot")
    with (
        patch.object(
            tmux_input_transport,
            "terminal_input_chunks",
            return_value=[b"first", b"second"],
        ),
        patch(
            "ccbot.tmux_input_transport._paste_chunk",
            new=AsyncMock(side_effect=[(True, False), (True, False)]),
        ) as paste,
        patch(
            "ccbot.tmux_input_transport._send_carriage_return",
            new=AsyncMock(return_value=True),
        ) as send_enter,
        patch("ccbot.tmux_input_transport.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("ccbot.tmux_input_transport.secrets.token_hex", return_value="op"),
    ):
        assert await manager.send_keys("@5", "весомый промпт", backend="codex")
    assert paste.await_args_list == [
        call("@5", "ccbot-op", b"first"),
        call("@5", "ccbot-op-1", b"second"),
    ]
    assert sleep.await_args_list == [call(0.025), call(0.025)]
    send_enter.assert_awaited_once_with("@5")


@pytest.mark.asyncio
async def test_special_key_uses_detached_tmux_command_client() -> None:
    with patch(
        "ccbot.tmux_input_transport._run_tmux",
        new=AsyncMock(return_value=(0, b"")),
    ) as run:
        assert await tmux_input_transport.send_special_key("@5", "Down", enter=True)
    run.assert_awaited_once_with("send-keys", "-t", "@5", "Down", "C-m")


@pytest.mark.asyncio
async def test_ambiguous_paste_is_not_retried_and_finalizes_once() -> None:
    manager = TmuxManager(session_name="ccbot")
    with (
        patch.object(
            tmux_input_transport,
            "terminal_input_chunks",
            return_value=[b"first", b"second", b"third"],
        ),
        patch(
            "ccbot.tmux_input_transport._paste_chunk",
            new=AsyncMock(side_effect=[(True, False), (False, True), (True, False)]),
        ) as paste,
        patch(
            "ccbot.tmux_input_transport._send_carriage_return",
            new=AsyncMock(return_value=True),
        ) as send_enter,
        patch("ccbot.tmux_input_transport.asyncio.sleep", new=AsyncMock()),
        patch("ccbot.tmux_input_transport.secrets.token_hex", return_value="op"),
    ):
        assert not await manager.send_keys("@5", "payload", backend="codex")
    assert paste.await_count == 3
    assert paste.await_args_list[-1] == call("@5", "ccbot-op-barrier", b" ")
    send_enter.assert_awaited_once_with("@5")


@pytest.mark.asyncio
async def test_bang_command_keeps_existing_tui_path() -> None:
    manager = TmuxManager(session_name="ccbot")
    with (
        patch("ccbot.tmux_manager.asyncio.sleep", new=AsyncMock()),
        patch(
            "ccbot.tmux_manager.tmux_input_transport.paste_literal",
            new=AsyncMock(return_value=True),
        ) as paste_literal,
        patch(
            "ccbot.tmux_manager.tmux_input_transport.send_special_key",
            new=AsyncMock(return_value=True),
        ) as send_enter,
    ):
        assert await manager.send_keys("@5", "!pwd", backend="claude")
    assert paste_literal.await_args_list == [call("@5", "!"), call("@5", "pwd")]
    send_enter.assert_awaited_once_with("@5", "", enter=True)
