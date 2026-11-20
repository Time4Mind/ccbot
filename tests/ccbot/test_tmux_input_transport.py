"""Literal terminal input is pasted in bounded UTF-8-safe chunks."""

import asyncio

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccbot import tmux_input_transport
from ccbot.tmux_manager import TmuxManager


@pytest.fixture
def running_codex(monkeypatch):
    monkeypatch.setattr(
        tmux_input_transport, "codex_process_is_running", AsyncMock(return_value=True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, allowed",
    [
        (b"zsh", False),
        (b"bash", False),
        (b"", False),
        (b"python", False),
        (b"codex", True),
        (b"node", True),
    ],
)
async def test_codex_request_never_reaches_a_shell_after_agent_exit(
    command, allowed
) -> None:
    manager = TmuxManager(session_name="ccbot")
    manager.capture_pane = AsyncMock(side_effect=["›", "› Пересчитай пайплайн"])
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(command, b""))
    with (
        patch(
            "ccbot.tmux_input_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        patch(
            "ccbot.tmux_input_transport._paste_chunk",
            new=AsyncMock(return_value=(True, False)),
        ) as paste,
        patch(
            "ccbot.tmux_input_transport._send_carriage_return",
            new=AsyncMock(return_value=True),
        ) as submit,
    ):
        assert (
            await manager.send_keys("@67", "Пересчитай пайплайн", backend="codex")
            is allowed
        )
    if not allowed:
        paste.assert_not_awaited()
        submit.assert_not_awaited()
    else:
        submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_exit_after_paste_never_submits_text_to_shell() -> None:
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(side_effect=[(b"codex", b""), (b"zsh", b"")])
    with (
        patch(
            "ccbot.tmux_input_transport.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        patch(
            "ccbot.tmux_input_transport._paste_chunk",
            new=AsyncMock(return_value=(True, False)),
        ),
        patch(
            "ccbot.tmux_input_transport._send_carriage_return",
            new=AsyncMock(return_value=True),
        ) as submit,
    ):
        manager = TmuxManager()
        manager.capture_pane = AsyncMock(side_effect=["›", "› request"])
        assert not await manager.send_keys("@67", "request", backend="codex")
    submit.assert_not_awaited()


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
async def test_multiline_paste_preserves_newlines_inside_bracketed_paste(
    running_codex,
) -> None:
    calls = []

    async def run_tmux(*args, input_bytes=None):
        calls.append((args, input_bytes))
        return 0, b""

    with patch("ccbot.tmux_input_transport._run_tmux", new=run_tmux):
        assert await tmux_input_transport.send_literal_chunked(
            "@5", "первая строка\n\nвторая 🙂", backend="codex"
        )

    assert calls[0][1] == "первая строка\n\nвторая 🙂 ".encode()
    assert calls[1][0][:3] == ("paste-buffer", "-d", "-p")
    assert "-r" in calls[1][0]
    assert calls[-1][0] == ("send-keys", "-t", "@5", "C-m")


@pytest.mark.asyncio
async def test_chunked_multiline_paste_keeps_order_and_one_submit(
    running_codex,
) -> None:
    text = "🙂" * 224 + "\n\n" + "строка" * 180
    calls = []

    async def run_tmux(*args, input_bytes=None):
        calls.append((args, input_bytes))
        return 0, b""

    with (
        patch("ccbot.tmux_input_transport._run_tmux", new=run_tmux),
        patch("ccbot.tmux_input_transport.asyncio.sleep", new=AsyncMock()),
    ):
        assert await tmux_input_transport.send_literal_chunked(
            "@5", text, backend="codex"
        )

    chunks = [payload for args, payload in calls if args[0] == "load-buffer"]
    pastes = [args for args, _ in calls if args[0] == "paste-buffer"]
    submits = [args for args, _ in calls if args[0] == "send-keys"]
    assert len(chunks) > 1
    assert b"".join(chunks).decode() == text + " "
    assert all(len(chunk) <= 900 for chunk in chunks)
    assert all("-p" in args and "-r" in args for args in pastes)
    assert submits == [("send-keys", "-t", "@5", "C-m")]


@pytest.mark.asyncio
async def test_literal_input_pastes_chunks_in_order_then_one_carriage_return(
    running_codex,
) -> None:
    manager = TmuxManager(session_name="ccbot")
    manager.capture_pane = AsyncMock(side_effect=["›", "› first", "› firstsecond"])
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
async def test_ambiguous_codex_paste_does_not_submit_partial_input(
    running_codex,
) -> None:
    manager = TmuxManager(session_name="ccbot")
    manager.capture_pane = AsyncMock(side_effect=["›", "› first"])
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
    assert paste.await_count == 2
    send_enter.assert_not_awaited()


@pytest.mark.asyncio
async def test_bang_command_keeps_existing_tui_path() -> None:
    manager = TmuxManager(session_name="ccbot")
    pane = MagicMock()

    class _Windows:
        def get(self, **_kwargs):
            return type("Window", (), {"active_pane": pane})()

    manager.get_session = lambda: type("Session", (), {"windows": _Windows()})()
    with (
        patch("ccbot.tmux_manager.asyncio.sleep", new=AsyncMock()),
        patch(
            "ccbot.tmux_manager.tmux_input_transport.send_literal_chunked",
            new=AsyncMock(return_value=True),
        ) as send_chunked,
    ):
        assert await manager.send_keys("@5", "!pwd", backend="claude")
    send_chunked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("opens_after_paste", [False, True])
async def test_codex_prompt_waits_for_approval_without_answering_it(
    monkeypatch, running_codex, opens_after_paste
):
    manager = TmuxManager()
    modal = not opens_after_paste
    draft = ""
    approvals_from_prompt = []
    submitted = []

    async def capture(_wid):
        nonlocal modal
        if modal:
            asyncio.get_running_loop().call_later(0.015, close_approval)
            return (
                "Would you like to run the following command?\n"
                "› 1. Yes, proceed (y)\n  2. No (esc)\n"
                "Press enter to confirm or esc to cancel"
            )
        return (
            f"› {draft or 'Ask Codex to do anything'}\n  GPT-6.2-Astra high · ~/workdir"
        )

    def close_approval():
        nonlocal modal
        modal = False

    async def paste(_wid, _name, chunk):
        nonlocal draft, modal
        if not modal:
            draft += chunk.decode()
        if opens_after_paste:
            modal = True
        return True, False

    async def enter(_wid):
        if modal:
            approvals_from_prompt.append(True)
        else:
            submitted.append(draft.strip())
        return True

    monkeypatch.setattr(manager, "capture_pane", capture)
    monkeypatch.setattr(tmux_input_transport, "_paste_chunk", paste)
    monkeypatch.setattr(tmux_input_transport, "_send_carriage_return", enter)
    assert await manager.send_keys("@134", "Тоже ок. Дальше.", backend="codex")
    assert not approvals_from_prompt
    assert submitted == ["Тоже ок. Дальше."]


@pytest.mark.asyncio
async def test_codex_does_not_press_enter_when_the_paste_disappeared(
    monkeypatch, running_codex
):
    manager = TmuxManager()
    manager.capture_pane = AsyncMock(return_value="› Ask Codex to do anything")
    monkeypatch.setattr(
        tmux_input_transport, "_paste_chunk", AsyncMock(return_value=(True, False))
    )
    enter = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_send_carriage_return", enter)
    assert not await manager.send_keys("@134", "исчезнувший текст", backend="codex")
    enter.assert_not_awaited()


def test_numbered_codex_draft_with_warning_footer_and_scrolled_long_draft():
    from ccbot.codex_input import composer, matches_draft

    pane = "› 1. Проверь статус\n\n  GPT-6.2-Sol high · ~/workdir\n  ⚠ 1 warning · f2 to view"
    assert composer(pane) == "1. Проверь статус"
    assert (
        composer(pane.replace("⚠ 1 warning · f2 to view", "tab to queue message"))
        == "1. Проверь статус"
    )
    tail = "строка " * 18
    assert matches_draft(tail, "начало " * 200 + tail, allow_folded=False)
    assert not matches_draft("статус", "1. Проверь статус", allow_folded=False)
