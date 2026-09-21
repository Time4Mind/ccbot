from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.bot import messages
from ccbot.bot import _messages_capture
from ccbot.bot.callbacks import _HANDLERS
from ccbot.bot.callbacks import confirm
from ccbot.bot.callbacks import footer
from ccbot.bot.commands import info
from ccbot.handlers import menu
from ccbot.handlers.callback_data import CB_CONF_CLEAR_YES
from ccbot.handlers.callback_data import CB_FT_TERM


class _Repost:
    def commit(self) -> None:
        pass


@asynccontextmanager
async def _bracket(*_args):
    yield _Repost()


def _remote_session() -> SimpleNamespace:
    return SimpleNamespace(
        id="session-1",
        name="Remote",
        state="active",
        node_id="worker-a",
        window_id="worker-a::@18",
        worker_session_id="routing-1",
        claude_session_id="provider-1",
        backend="claude",
    )


@pytest.mark.asyncio
async def test_forwarded_slash_command_never_uses_leader_tmux(monkeypatch) -> None:
    sess = _remote_session()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(text="/model"),
    )
    context = SimpleNamespace(bot=object())
    sent = AsyncMock(return_value=(True, "ok"))
    local_lookup = AsyncMock(side_effect=AssertionError("remote id reached local tmux"))
    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: sess.window_id)
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "session_is_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", _bracket)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent)
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=local_lookup),
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_display_name=lambda _wid: "Remote",
        ),
    )

    assert await messages.forward_command_handler(update, context)
    sent.assert_awaited_once_with(sess.window_id, "/model", sess)
    local_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwarded_clear_resets_worker_provider_binding(monkeypatch) -> None:
    sess = _remote_session()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(text="/clear"),
    )
    context = SimpleNamespace(bot=object())
    sent = AsyncMock(return_value=(True, "ok"))
    reset = AsyncMock(return_value=True)
    clear_window = MagicMock()
    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: sess.window_id)
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "session_is_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", _bracket)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent)
    monkeypatch.setattr(messages, "reset_session_binding", reset)
    monkeypatch.setattr(messages, "clear_card", AsyncMock())
    monkeypatch.setattr(messages, "resume_card_view", AsyncMock())
    monkeypatch.setattr(messages, "safe_reply", AsyncMock())
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_display_name=lambda _wid: "Remote",
            clear_window_session=clear_window,
        ),
    )

    assert await messages.forward_command_handler(update, context)
    sent.assert_awaited_once_with(sess.window_id, "/clear", sess)
    reset.assert_awaited_once_with(sess)
    clear_window.assert_called_once_with(sess.window_id)


@pytest.mark.asyncio
async def test_pending_ui_guard_captures_worker_pane(monkeypatch) -> None:
    sess = _remote_session()
    pane = (
        "Would you like to run this command?\n"
        "› 1. Yes, proceed\n"
        "  2. No\n"
        "Press enter to confirm or esc to cancel"
    )
    runtime = SimpleNamespace(
        capture_session=AsyncMock(return_value={"ok": True, "pane": pane})
    )
    enter_kb = AsyncMock()
    local_lookup = AsyncMock(side_effect=AssertionError("remote id reached local tmux"))
    monkeypatch.setattr(messages, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(
        messages, "tmux_manager", SimpleNamespace(find_window_by_id=local_lookup)
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_active_session=lambda _uid: sess,
        ),
    )
    monkeypatch.setattr(messages, "enter_kb_mode", enter_kb)
    monkeypatch.setattr(messages, "safe_reply", AsyncMock())

    intercepted = await messages._intercept_if_pending_ui(
        object(), 42, sess.window_id, object()
    )

    assert intercepted is True
    runtime.capture_session.assert_awaited_once_with(
        "worker-a", "routing-1", with_ansi=False
    )
    enter_kb.assert_awaited_once()
    local_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_clear_uses_worker_escape_then_clear(monkeypatch) -> None:
    sess = _remote_session()
    manager = SimpleNamespace(
        get_session=lambda _sid: sess,
        send_to_window=AsyncMock(return_value=(True, "ok")),
        clear_window_session=MagicMock(),
    )
    query = SimpleNamespace(
        data=f"{CB_CONF_CLEAR_YES}{sess.id}",
        answer=AsyncMock(),
        get_bot=lambda: object(),
    )
    send_key = AsyncMock(return_value=True)
    reset_binding = AsyncMock(return_value=True)
    monkeypatch.setattr(confirm, "session_manager", manager)
    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", send_key)
    monkeypatch.setattr("ccbot.terminal_runtime.reset_session_binding", reset_binding)
    monkeypatch.setattr("ccbot.handlers.notifications.clear_card", AsyncMock())
    monkeypatch.setattr("ccbot.handlers.notifications.resume_card_view", AsyncMock())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    assert await confirm.handle(query, SimpleNamespace(), SimpleNamespace(id=42))
    send_key.assert_awaited_once_with(sess, "Escape")
    reset_binding.assert_awaited_once_with(sess)
    manager.send_to_window.assert_awaited_once_with(sess.window_id, "/clear")


@pytest.mark.asyncio
async def test_claude_usage_uses_worker_terminal_adapter(monkeypatch) -> None:
    sess = _remote_session()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42), message=SimpleNamespace()
    )
    reply = AsyncMock()
    capture = AsyncMock(return_value="Current session 50% left")
    send_key = AsyncMock(return_value=True)
    manager = SimpleNamespace(
        get_active_session=lambda _uid: sess,
        send_to_window=AsyncMock(return_value=(True, "ok")),
    )
    monkeypatch.setattr(info, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(info, "session_manager", manager)
    monkeypatch.setattr(info, "active_window", lambda _uid: sess.window_id)
    monkeypatch.setattr(info, "safe_reply", reply)
    monkeypatch.setattr(info, "capture_session_pane", capture)
    monkeypatch.setattr(info, "send_session_key", send_key)
    monkeypatch.setattr(info.asyncio, "sleep", AsyncMock())

    await info.usage_command(update, SimpleNamespace())

    capture.assert_awaited_once_with(sess)
    assert send_key.await_args_list[0].args == (sess, "Escape")
    assert "50% left" in reply.await_args.args[1]


def test_local_terminal_is_truthfully_hidden_for_worker(monkeypatch) -> None:
    sess = _remote_session()
    manager = SimpleNamespace(
        get_active_session=lambda _uid: sess,
        get_user_settings=lambda _uid: {"option_button_terminal": True},
    )
    monkeypatch.setattr(menu, "session_manager", manager)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    local_probe = MagicMock(return_value=False)
    monkeypatch.setattr(
        "ccbot.tmux_manager.tmux_manager.has_client_for_window", local_probe
    )

    assert menu.can_offer_terminal(42) is False
    local_probe.assert_not_called()


@pytest.mark.asyncio
async def test_stale_terminal_tap_on_worker_returns_truthful_alert(
    monkeypatch,
) -> None:
    sess = _remote_session()
    answer = AsyncMock()
    query = SimpleNamespace(data=CB_FT_TERM, answer=answer)
    monkeypatch.setattr(
        footer,
        "session_manager",
        SimpleNamespace(get_active_session=lambda _uid: sess),
    )
    open_terminal = AsyncMock()
    monkeypatch.setattr("ccbot.local_terminal.open_terminal_for_window", open_terminal)

    assert await footer.handle(query, SimpleNamespace(), SimpleNamespace(id=42))
    answer.assert_awaited_once_with(
        "Terminal is available only for sessions on this machine.", show_alert=True
    )
    open_terminal.assert_not_awaited()


def test_legacy_history_pagination_callback_is_not_registered() -> None:
    assert all(
        handler.__module__ != "ccbot.bot.callbacks.history_pagination"
        for handler in _HANDLERS
    )


@pytest.mark.asyncio
async def test_bash_capture_runs_for_worker_session(monkeypatch) -> None:
    sess = _remote_session()
    captured = asyncio.Event()

    async def capture(_bot, _user_id, _wid, command):
        assert command == "pwd"
        captured.set()

    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(find_session_by_window=lambda _wid: sess),
    )
    monkeypatch.setattr(messages, "_capture_bash_output", capture)

    messages._maybe_start_bash_capture(object(), 42, sess.window_id, "!pwd")
    await asyncio.wait_for(captured.wait(), timeout=1)
    await asyncio.gather(*messages._bash_capture_tasks.values())


@pytest.mark.asyncio
async def test_reply_quote_routes_to_live_worker_without_local_tmux(
    monkeypatch,
) -> None:
    sess = _remote_session()
    update = SimpleNamespace(
        message=SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=10),
        )
    )
    manager = SimpleNamespace(
        get_session=lambda _sid: sess,
        get_active_session=lambda _uid: None,
        send_to_window=AsyncMock(return_value=(True, "ok")),
        touch_session=MagicMock(),
    )
    monkeypatch.setattr(_messages_capture, "session_manager", manager)
    monkeypatch.setattr(
        _messages_capture, "lookup_session_for_message", lambda _uid, _mid: sess.id
    )
    monkeypatch.setattr(
        _messages_capture, "session_is_reachable", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(_messages_capture, "get_card_state", MagicMock())
    reply = AsyncMock()
    monkeypatch.setattr(_messages_capture, "safe_reply", reply)
    local_lookup = AsyncMock(side_effect=AssertionError("remote id reached local tmux"))
    monkeypatch.setattr(
        _messages_capture,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=local_lookup),
    )

    assert await _messages_capture.route_reply_quote(update, 42, "continue")
    manager.send_to_window.assert_awaited_once_with(sess.window_id, "continue")
    local_lookup.assert_not_awaited()
