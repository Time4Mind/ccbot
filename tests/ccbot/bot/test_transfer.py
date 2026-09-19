from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ccbot.bot.callbacks import transfer
from ccbot.handlers.callback_data import (
    CB_FT_TRANSFER,
    CB_TR_BACKEND,
    CB_TR_CONFIRM,
    CB_TR_NODE,
)
from ccbot.node_models import Node
from ccbot.session_models import Session
from ccbot.transfer_models import SessionTransfer
from ccbot.transfer_runtime import TransferRuntimeResult


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _query(data: str):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(message_id=91),
        answer=AsyncMock(),
    )


def _context():
    return SimpleNamespace(user_data={}, bot=SimpleNamespace())


def _user_settings(_user_id: int):
    return {"language": "ru", "option_button_transfer": True}


def _session(*, node_id: str = "local") -> Session:
    return Session(
        id="source1",
        name="Рабочая сессия",
        node_id=node_id,
        state="idle",
        backend="claude",
    )


def test_transfer_node_picker_hides_unavailable_nodes_and_backends(monkeypatch):
    source = _session()
    monkeypatch.setattr(transfer.session_manager, "get_active_session", lambda _uid: source)
    monkeypatch.setattr(
        transfer.session_manager,
        "get_session",
        lambda session_id: source if session_id == source.id else None,
    )
    monkeypatch.setattr(
        transfer.session_manager,
        "get_user_settings",
        _user_settings,
    )
    monkeypatch.setattr(
        transfer.session_manager,
        "list_nodes",
        lambda: [
            Node.local(),
            Node("ready", "Рабочий Mac", state="ready", backends=["codex"]),
            Node("offline", "Оффлайн", state="offline", backends=["claude"]),
            Node("pending", "Подключается", state="pending", backends=["codex"]),
            Node("empty", "Без backend", state="ready", backends=[]),
        ],
    )

    markup = transfer.build_transfer_node_keyboard(42)
    callbacks = _callbacks(markup)

    assert f"{CB_TR_NODE}ready" in callbacks
    assert f"{CB_TR_NODE}offline" not in callbacks
    assert f"{CB_TR_NODE}pending" not in callbacks
    assert f"{CB_TR_NODE}empty" not in callbacks
    assert f"{CB_TR_NODE}local" not in callbacks


@pytest.mark.asyncio
async def test_transfer_flow_is_node_then_backend_then_confirmation(monkeypatch):
    source = _session()
    target = Node("remote", "Удалённый Mac", state="ready", backends=["codex"])
    monkeypatch.setattr(transfer.session_manager, "get_active_session", lambda _uid: source)
    monkeypatch.setattr(
        transfer.session_manager,
        "get_session",
        lambda session_id: source if session_id == source.id else None,
    )
    monkeypatch.setattr(transfer.session_manager, "get_user_settings", _user_settings)
    monkeypatch.setattr(
        type(transfer.session_manager),
        "has_multiple_nodes",
        property(lambda _self: True),
    )
    monkeypatch.setattr(transfer, "_card_is_busy", lambda _state: False)
    monkeypatch.setattr(
        transfer.session_manager,
        "list_nodes",
        lambda: [Node.local(), target],
    )
    monkeypatch.setattr(
        transfer.session_manager,
        "get_node",
        lambda node_id: target if node_id == target.id else source_node(source),
    )
    monkeypatch.setattr(transfer, "set_view", AsyncMock())

    context = _context()
    user = SimpleNamespace(id=42)

    query = _query(CB_FT_TRANSFER)
    assert await transfer.handle(query, context, user)
    assert context.user_data[transfer.SOURCE_SESSION_KEY] == source.id
    assert context.user_data[transfer.TARGET_NODE_KEY] is None
    assert transfer.set_view.await_args.args[3].startswith("*Перенос")

    query = _query(f"{CB_TR_NODE}{target.id}")
    assert await transfer.handle(query, context, user)
    assert context.user_data[transfer.TARGET_NODE_KEY] == target.id
    assert transfer.set_view.await_args.args[3].startswith("*Выбери бэкенд")
    assert f"{CB_TR_BACKEND}{target.id}:codex" in _callbacks(
        transfer.set_view.await_args.args[4]
    )

    query = _query(f"{CB_TR_BACKEND}{target.id}:codex")
    assert await transfer.handle(query, context, user)
    assert context.user_data[transfer.TARGET_BACKEND_KEY] == "codex"
    assert transfer.set_view.await_args.args[3].startswith("*Подтвердить")
    assert CB_TR_CONFIRM in _callbacks(transfer.set_view.await_args.args[4])


def source_node(source: Session) -> Node:
    return Node(source.node_id, source.node_id, state="ready", backends=["claude"])


@pytest.mark.asyncio
async def test_finish_transfer_archives_source_and_binds_target_queue(monkeypatch):
    source = _session()
    transfer_record = SessionTransfer(
        id="transfer1",
        source_session_id=source.id,
        source_node_id="local",
        target_node_id="remote",
        target_backend="codex",
        context_path="/tmp/full-context.md",
    )
    target = Session(
        id="target1",
        name=source.name,
        state="active",
        node_id="remote",
        backend="codex",
        window_id="remote-window",
    )
    delivered = AsyncMock(return_value=True)
    runtime = SimpleNamespace(
        start_context_transfer=AsyncMock(
            return_value=TransferRuntimeResult(
                target_window_id=target.window_id,
                delivery=delivered,
            )
        )
    )
    monkeypatch.setattr(
        transfer.session_manager,
        "transfers",
        {transfer_record.id: transfer_record},
    )
    monkeypatch.setattr(
        transfer.session_manager,
        "get_session",
        lambda session_id: source if session_id == source.id else None,
    )
    monkeypatch.setattr(transfer, "get_node_runtime", lambda _node_id: runtime)
    complete = Mock(return_value=target)
    monkeypatch.setattr(transfer.session_manager, "complete_context_transfer", complete)
    monkeypatch.setattr(transfer, "bind_transfer_queue", lambda _uid, delivery: delivery)
    paint = AsyncMock()
    monkeypatch.setattr(transfer, "paint_card_on_carrier", paint)

    query = _query("unused")
    bot = SimpleNamespace()
    await transfer._finish_transfer(
        user_id=42,
        bot=bot,
        query=query,
        transfer_id=transfer_record.id,
    )

    runtime.start_context_transfer.assert_awaited_once()
    complete.assert_called_once_with(
        transfer_record.id,
        user_id=42,
        context_error="",
        target_window_id="remote-window",
        target_workdir="",
        target_agent_session_id="",
    )
    paint.assert_awaited_once_with(bot, 42, target, 91)
