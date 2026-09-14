"""Literal terminal input is pasted in bounded UTF-8-safe chunks."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccbot.tmux_manager import TmuxManager


def test_terminal_input_chunks_preserve_utf8_and_byte_limit() -> None:
    text = "абв🙂" * 400
    chunks = TmuxManager._terminal_input_chunks(text, backend="claude")
    assert b"".join(chunks).decode() == text
    assert all(len(chunk) <= 900 for chunk in chunks)
    assert len(chunks) > 1


def test_codex_transport_adds_only_trailing_space() -> None:
    text = "проверь @README"
    chunks = TmuxManager._terminal_input_chunks(text, backend="codex")
    assert b"".join(chunks).decode() == text + " "


@pytest.mark.asyncio
async def test_literal_input_pastes_chunks_in_order_then_one_carriage_return() -> None:
    manager = TmuxManager(session_name="ccbot")
    manager._paste_literal_chunk = AsyncMock(side_effect=[(True, False), (True, False)])
    manager._send_carriage_return = AsyncMock(return_value=True)
    with (
        patch.object(
            manager, "_terminal_input_chunks", return_value=[b"first", b"second"]
        ),
        patch("ccbot.tmux_manager.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("ccbot.tmux_manager.secrets.token_hex", return_value="op"),
    ):
        assert await manager.send_keys("@5", "весомый промпт", backend="codex")
    assert manager._paste_literal_chunk.await_args_list == [
        call("@5", "ccbot-op", b"first"),
        call("@5", "ccbot-op-1", b"second"),
    ]
    assert sleep.await_args_list == [call(0.025), call(0.025)]
    manager._send_carriage_return.assert_awaited_once_with("@5")


@pytest.mark.asyncio
async def test_ambiguous_paste_is_not_retried_and_finalizes_once() -> None:
    manager = TmuxManager(session_name="ccbot")
    manager._paste_literal_chunk = AsyncMock(
        side_effect=[(True, False), (False, True), (True, False)]
    )
    manager._send_carriage_return = AsyncMock(return_value=True)
    with (
        patch.object(
            manager,
            "_terminal_input_chunks",
            return_value=[b"first", b"second", b"third"],
        ),
        patch("ccbot.tmux_manager.asyncio.sleep", new=AsyncMock()),
        patch("ccbot.tmux_manager.secrets.token_hex", return_value="op"),
    ):
        assert not await manager.send_keys("@5", "payload", backend="codex")
    assert manager._paste_literal_chunk.await_count == 3
    assert manager._paste_literal_chunk.await_args_list[-1] == call(
        "@5", "ccbot-op-barrier", b" "
    )
    manager._send_carriage_return.assert_awaited_once_with("@5")


@pytest.mark.asyncio
async def test_bang_command_keeps_existing_tui_path() -> None:
    manager = TmuxManager(session_name="ccbot")
    manager._send_literal_chunked = AsyncMock(return_value=True)
    pane = MagicMock()

    class _Windows:
        def get(self, **_kwargs):
            return type("Window", (), {"active_pane": pane})()

    manager.get_session = lambda: type("Session", (), {"windows": _Windows()})()
    with patch("ccbot.tmux_manager.asyncio.sleep", new=AsyncMock()):
        assert await manager.send_keys("@5", "!pwd", backend="claude")
    manager._send_literal_chunked.assert_not_awaited()
