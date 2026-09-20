from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.remote_prompt_queue import RemotePromptQueue
from ccbot.node_models import Node


@pytest.mark.asyncio
async def test_offline_prompts_are_visible_and_drain_in_fifo_order(monkeypatch):
    available = False
    delivered: list[str] = []
    receipts = [SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    reply = AsyncMock(side_effect=receipts)
    edit = AsyncMock()
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_reply", reply)
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_edit", edit)
    queue = RemotePromptQueue(
        node_available=lambda _node_id: available,
        autostart=False,
        now=lambda: 100.0,
    )

    async def deliver(text: str) -> bool:
        delivered.append(text)
        return True

    assert await queue.admit(
        original_message=object(),
        session_id="session-1",
        node_id="worker-a",
        node_name="Worker A",
        deliver=lambda: deliver("first"),
    )
    assert await queue.admit(
        original_message=object(),
        session_id="session-1",
        node_id="worker-a",
        node_name="Worker A",
        deliver=lambda: deliver("second"),
    )
    assert "позиция 1" in reply.await_args_list[0].args[1]
    assert "позиция 2" in reply.await_args_list[1].args[1]

    assert await queue.drain_once("session-1") == 0
    available = True
    assert await queue.drain_once("session-1") == 2
    assert delivered == ["first", "second"]
    assert edit.await_count == 2
    assert all("Передано" in call.args[1] for call in edit.await_args_list)


@pytest.mark.asyncio
async def test_queue_expires_after_fifteen_minutes(monkeypatch):
    now = 100.0
    receipt = SimpleNamespace(message_id=1)
    monkeypatch.setattr(
        "ccbot.remote_prompt_queue.safe_reply", AsyncMock(return_value=receipt)
    )
    edit = AsyncMock()
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_edit", edit)
    queue = RemotePromptQueue(
        node_available=lambda _node_id: False,
        autostart=False,
        now=lambda: now,
    )

    assert await queue.admit(
        original_message=object(),
        session_id="session-1",
        node_id="worker-a",
        node_name="Worker A",
        deliver=AsyncMock(return_value=True),
    )
    now += 15 * 60

    assert await queue.drain_once("session-1") == 0
    assert not queue.has_pending("session-1")
    assert "Не отправлено" in edit.await_args.args[1]


@pytest.mark.asyncio
async def test_queue_rejects_eleventh_prompt(monkeypatch):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=1))
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_reply", reply)
    queue = RemotePromptQueue(
        node_available=lambda _node_id: False,
        autostart=False,
    )

    for _ in range(10):
        assert await queue.admit(
            original_message=object(),
            session_id="session-1",
            node_id="worker-a",
            node_name="Worker A",
            deliver=AsyncMock(return_value=True),
        )

    assert not await queue.admit(
        original_message=object(),
        session_id="session-1",
        node_id="worker-a",
        node_name="Worker A",
        deliver=AsyncMock(return_value=True),
    )
    assert "уже 10" in reply.await_args.args[1]


@pytest.mark.asyncio
async def test_queue_enforces_global_limit_across_sessions(monkeypatch):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=1))
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_reply", reply)
    queue = RemotePromptQueue(
        node_available=lambda _node_id: False,
        autostart=False,
        global_limit=2,
    )

    for session_id in ("session-1", "session-2"):
        assert await queue.admit(
            original_message=object(),
            session_id=session_id,
            node_id="worker-a",
            node_name="Worker A",
            deliver=AsyncMock(return_value=True),
        )

    assert not await queue.admit(
        original_message=object(),
        session_id="session-3",
        node_id="worker-a",
        node_name="Worker A",
        deliver=AsyncMock(return_value=True),
    )
    assert "общая очередь" in reply.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_shutdown_marks_every_waiting_prompt_as_not_sent(monkeypatch):
    receipts = [SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    monkeypatch.setattr(
        "ccbot.remote_prompt_queue.safe_reply", AsyncMock(side_effect=receipts)
    )
    edit = AsyncMock()
    monkeypatch.setattr("ccbot.remote_prompt_queue.safe_edit", edit)
    queue = RemotePromptQueue(
        node_available=lambda _node_id: False,
        autostart=False,
    )
    deliveries = [AsyncMock(return_value=True), AsyncMock(return_value=True)]
    for delivery in deliveries:
        assert await queue.admit(
            original_message=object(),
            session_id="session-1",
            node_id="worker-a",
            node_name="Worker A",
            deliver=delivery,
        )

    await queue.fail_all()

    assert not queue.has_pending("session-1")
    assert edit.await_count == 2
    assert all(
        call.args[1] == "❌ Не отправлено в сессию: ccbot перезапущен"
        for call in edit.await_args_list
    )
    assert all(delivery.await_count == 0 for delivery in deliveries)


@pytest.mark.asyncio
async def test_remote_dispatch_queues_before_preprocessing(monkeypatch):
    from ccbot.bot import messages

    update = MagicMock()
    update.message = MagicMock()
    context = MagicMock()
    session = SimpleNamespace(
        id="session-1",
        node_id="worker-a",
        window_id="worker-a::@1",
    )
    manager = MagicMock()
    manager.find_session_by_window.return_value = session
    manager.send_to_window = AsyncMock()
    manager.get_node.return_value = Node(
        id="worker-a",
        display_name="Worker A",
        state="ready",
        last_seen_at=1.0,
    )
    queue = MagicMock()
    queue.has_pending.return_value = False
    queue.admit = AsyncMock(return_value=True)
    preprocess = AsyncMock(side_effect=AssertionError("must wait for node"))
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(messages, "remote_prompt_queue", queue, raising=False)
    monkeypatch.setattr(messages, "prepare_request_for_dispatch", preprocess)

    assert await messages._dispatch_text_to_active(
        update, context, 42, "worker-a::@1", "hello"
    )

    queue.admit.assert_awaited_once()
    preprocess.assert_not_awaited()
    manager.send_to_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_dispatch_reuses_preprocessing_after_delivery_retry(monkeypatch):
    from ccbot.bot import messages

    update = MagicMock()
    update.message = MagicMock()
    context = MagicMock()
    session = SimpleNamespace(
        id="session-1",
        node_id="worker-a",
        window_id="worker-a::@1",
    )
    manager = MagicMock()
    manager.find_session_by_window.return_value = session
    manager.get_node.return_value = Node(
        id="worker-a",
        display_name="Worker A",
        state="ready",
        last_seen_at=1.0,
    )
    queue = MagicMock()
    queue.has_pending.return_value = True
    queued: dict[str, object] = {}

    async def admit(**kwargs):
        queued["deliver"] = kwargs["deliver"]
        return True

    queue.admit = AsyncMock(side_effect=admit)
    prepared = SimpleNamespace(text="prepared once", confirm_delivery=MagicMock())
    preprocess = AsyncMock(return_value=prepared)
    send = AsyncMock(return_value=(False, "offline"))
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(messages, "remote_prompt_queue", queue, raising=False)
    monkeypatch.setattr(messages, "prepare_request_for_dispatch", preprocess)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", send)
    monkeypatch.setattr(messages, "safe_reply", AsyncMock())

    assert await messages._dispatch_text_to_active(
        update, context, 42, "worker-a::@1", "original"
    )
    delivery = queued["deliver"]
    assert callable(delivery)
    assert not await delivery()
    assert not await delivery()

    preprocess.assert_awaited_once()
    assert preprocess.await_args.kwargs["persist_recovery"] is False
    assert [call.args[1] for call in send.await_args_list] == [
        "prepared once",
        "prepared once",
    ]
