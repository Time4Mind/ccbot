from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import node_runtime
from ccbot.bot import session_events
from ccbot.node_runtime import NodeRpcClient
from ccbot.session_models import Session
from ccbot.session import session_manager
from ccbot.node_transport import NodeEnvelope
from ccbot.node_models import Node


@pytest.mark.asyncio
async def test_worker_health_auto_registers_remote_node_and_runtime(monkeypatch):
    rpc = SimpleNamespace(close=AsyncMock())
    captured: dict[str, object] = {}

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    registered_nodes = []
    persisted = []
    registered_runtimes = []
    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    known_nodes = {}
    monkeypatch.setattr(
        session_manager,
        "get_node",
        lambda node_id: known_nodes.get(node_id),
        raising=False,
    )
    monkeypatch.setattr(
        session_manager,
        "register_node",
        lambda node, *, persist=True: (
            known_nodes.__setitem__(node.id, node),
            registered_nodes.append(node),
            persisted.append(persist),
        )[0],
        raising=False,
    )
    monkeypatch.setattr(
        node_runtime,
        "register_node_runtime",
        lambda node_id, _runtime: registered_runtimes.append(node_id),
    )
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["local"],
    )
    handler = captured["event_handler"]
    await handler(
        NodeEnvelope(
            kind="health",
            payload={
                "node_id": "worker-a",
                "display_name": "Worker A",
                "state": "ready",
                "backends": ["codex"],
                "capabilities": {"context_transfer": True},
                "ssh": {
                    "host": "127.0.0.1",
                    "user": "worker",
                    "port": 22041,
                    "proxy_jump": "bastion",
                },
            },
        )
    )

    assert registered_nodes[0].id == "worker-a"
    assert registered_nodes[0].state == "ready"
    assert registered_nodes[0].ssh_host == "127.0.0.1"
    assert registered_nodes[0].ssh_user == "worker"
    assert registered_nodes[0].ssh_port == 22041
    assert registered_nodes[0].ssh_proxy_jump == "bastion"
    await handler(
        NodeEnvelope(
            kind="health",
            payload={
                "node_id": "worker-a",
                "display_name": "Worker A",
                "state": "ready",
                "backends": ["codex"],
                "capabilities": {"context_transfer": True},
            },
        )
    )
    assert persisted == [True, False]
    assert registered_runtimes == ["worker-a"]
    await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_worker_binding_event_persists_native_provider_identity(monkeypatch):
    rpc = SimpleNamespace(close=AsyncMock())
    captured: dict[str, object] = {}

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(node_runtime, "register_node_runtime", lambda *_args: None)
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()
    session_manager.register_node(
        Node(id="worker-a", display_name="Worker A", state="ready")
    )
    session = session_manager.create_session(
        name="Remote",
        node_id="worker-a",
        worker_session_id="routing-42",
        backend="codex",
    )

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["worker-a"],
    )
    handler = captured["event_handler"]
    await handler(
        NodeEnvelope(
            kind="event",
            payload={
                "node_id": "worker-a",
                "event_type": "session_binding",
                "session_id": "routing-42",
                "provider_session_id": "provider-01",
                "transcript_path": "/worker/rollout.jsonl",
            },
        )
    )

    assert session.worker_session_id == "routing-42"
    assert session.claude_session_id == "provider-01"
    assert session.provider_transcript_path == "/worker/rollout.jsonl"
    await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_worker_live_image_event_reaches_leader_handler(monkeypatch):
    rpc = SimpleNamespace(close=AsyncMock())
    captured: dict[str, object] = {}
    received = []

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    async def receive(message):
        received.append(message)

    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(node_runtime, "register_node_runtime", lambda *_args: None)
    node_runtime.set_remote_message_handler(receive)
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()
    session_manager.register_node(
        Node(id="worker-a", display_name="Worker A", state="ready")
    )
    session_manager.create_session(
        name="Remote",
        node_id="worker-a",
        worker_session_id="routing-42",
        backend="codex",
    )

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["worker-a"],
    )
    handler = captured["event_handler"]
    await handler(
        NodeEnvelope(
            kind="event",
            payload={
                "node_id": "worker-a",
                "event_type": "session_message",
                "session_id": "routing-42",
                "role": "assistant",
                "text": "",
                "content_type": "tool_result",
                "image_data": [
                    {
                        "media_type": "image/png",
                        "data": base64.b64encode(b"png-bytes").decode(),
                    }
                ],
            },
        )
    )

    try:
        assert received[0].image_data == [("image/png", b"png-bytes")]
    finally:
        node_runtime.set_remote_message_handler(None)
        await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_worker_event_uses_same_telegram_card_callback_as_local(monkeypatch):
    rpc = SimpleNamespace(close=AsyncMock())
    captured: dict[str, object] = {}
    session = Session(
        id="remote-session",
        name="Remote",
        window_id="@8",
        workdir="/worker",
        state="active",
        claude_session_id="routing-42",
        node_id="worker-a",
    )
    update_card = AsyncMock()

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    async def route_to_card(message):
        await session_events.handle_new_message(message, AsyncMock())

    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(node_runtime, "register_node_runtime", lambda *_args: None)
    monkeypatch.setattr(
        session_events.session_manager,
        "all_user_sessions_with_claude_id",
        lambda _session_id: [(42, session)],
    )
    monkeypatch.setattr(
        session_events.session_manager, "touch_session", lambda _session_id: None
    )
    monkeypatch.setattr(session_events, "is_active_for_user", lambda *_args: True)
    monkeypatch.setattr(session_events, "fire_typing", AsyncMock())
    monkeypatch.setattr(session_events, "update_session_card", update_card)
    node_runtime.set_remote_message_handler(route_to_card)
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["worker-a"],
    )
    handler = captured["event_handler"]
    await handler(
        NodeEnvelope(
            kind="event",
            payload={
                "node_id": "worker-a",
                "event_type": "session_message",
                "session_id": "routing-42",
                "role": "assistant",
                "text": "remote answer",
                "content_type": "text",
            },
        )
    )

    try:
        update_card.assert_awaited_once()
        assert update_card.await_args.args[3].text == "remote answer"
    finally:
        node_runtime.set_remote_message_handler(None)
        await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_health_mismatch_requests_exact_leader_revision_once(monkeypatch):
    rpc = SimpleNamespace(
        close=AsyncMock(), request=AsyncMock(return_value={"ok": True})
    )
    captured: dict[str, object] = {}

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(node_runtime, "current_git_revision", lambda: "b" * 40)
    monkeypatch.setattr(node_runtime.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(session_manager, "get_node", lambda _node_id: None)
    monkeypatch.setattr(
        session_manager, "register_node", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(node_runtime, "register_node_runtime", lambda *_args: None)
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()
    for task in node_runtime._node_update_tasks.values():
        task.cancel()
    node_runtime._node_update_tasks.clear()
    node_runtime._node_update_attempts.clear()

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["local"],
    )
    handler = captured["event_handler"]
    health = NodeEnvelope(
        kind="health",
        payload={
            "node_id": "worker-a",
            "state": "ready",
            "backends": ["codex"],
            "ccbot_version": "a" * 40,
        },
    )
    await handler(health)
    await handler(health)
    await asyncio.gather(*tuple(node_runtime._node_update_tasks.values()))

    rpc.request.assert_awaited_once_with(
        "worker-a",
        "update_runtime",
        {"revision": "b" * 40},
        retries=0,
        timeout=600.0,
    )
    await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_matching_or_unknown_version_does_not_request_update(monkeypatch):
    rpc = SimpleNamespace(
        close=AsyncMock(), request=AsyncMock(return_value={"ok": True})
    )
    captured: dict[str, object] = {}

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(node_runtime, "current_git_revision", lambda: "b" * 40)
    monkeypatch.setattr(session_manager, "get_node", lambda _node_id: None)
    monkeypatch.setattr(
        session_manager, "register_node", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(node_runtime, "register_node_runtime", lambda *_args: None)
    node_runtime._leader_rpc = None
    node_runtime._remote_node_ids.clear()

    await node_runtime.connect_configured_remote_runtimes(
        relay_url="relay.example.test:8765",
        leader_id="leader",
        secret="secret",
        node_ids=["local"],
    )
    handler = captured["event_handler"]
    for version in ("b" * 40, ""):
        await handler(
            NodeEnvelope(
                kind="health",
                payload={
                    "node_id": "worker-a",
                    "state": "ready",
                    "backends": ["codex"],
                    "ccbot_version": version,
                },
            )
        )
    await asyncio.sleep(0)

    rpc.request.assert_not_awaited()
    await node_runtime.shutdown_remote_runtimes()


@pytest.mark.asyncio
async def test_rpc_result_is_not_blocked_by_slow_event_handler():
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.sent: list[NodeEnvelope] = []

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    event_started = asyncio.Event()
    release_event = asyncio.Event()

    async def slow_handler(_message: NodeEnvelope) -> None:
        event_started.set()
        await release_event.wait()

    transport = QueueTransport()
    client = NodeRpcClient(transport, event_handler=slow_handler, request_timeout=1)
    request = asyncio.create_task(client.request("worker-a", "health", retries=0))
    while not transport.sent:
        await asyncio.sleep(0)
    request_id = transport.sent[0].request_id
    await transport.incoming.put(NodeEnvelope(kind="event", payload={"node_id": "a"}))
    await asyncio.wait_for(event_started.wait(), timeout=1)
    await transport.incoming.put(
        NodeEnvelope(kind="result", request_id=request_id, payload={"ok": True})
    )

    assert await asyncio.wait_for(request, timeout=0.2) == {"ok": True}
    release_event.set()
    await client.close()


@pytest.mark.asyncio
async def test_rpc_result_is_not_blocked_by_saturated_event_queue():
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.sent: list[NodeEnvelope] = []

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def blocked_handler(_message: NodeEnvelope) -> None:
        handler_started.set()
        await release_handler.wait()

    transport = QueueTransport()
    client = NodeRpcClient(transport, event_handler=blocked_handler, request_timeout=1)
    client._event_queue = asyncio.Queue(maxsize=1)
    request = asyncio.create_task(client.request("worker-a", "health", retries=0))
    while not transport.sent:
        await asyncio.sleep(0)
    request_id = transport.sent[0].request_id
    for sequence in (1, 2, 3):
        await transport.incoming.put(
            NodeEnvelope(
                kind="event",
                sequence=sequence,
                payload={"node_id": "worker-a", "event_id": f"event-{sequence}"},
            )
        )
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await transport.incoming.put(
        NodeEnvelope(kind="result", request_id=request_id, payload={"ok": True})
    )

    try:
        assert await asyncio.wait_for(request, timeout=0.2) == {"ok": True}
    finally:
        release_handler.set()
        await client.close()


@pytest.mark.asyncio
async def test_applied_worker_event_is_acked_and_deduplicated_after_leader_restart(
    tmp_path,
):
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.sent: list[NodeEnvelope] = []
            self.acked = asyncio.Event()

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)
            if message.kind == "ack":
                self.acked.set()

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    handled: list[str] = []

    async def handler(message: NodeEnvelope) -> None:
        handled.append(str((message.payload or {}).get("text", "")))

    state_path = tmp_path / "leader-events.json"
    event = NodeEnvelope(
        kind="event",
        request_id="worker-a:1",
        sequence=1,
        payload={"node_id": "worker-a", "text": "once"},
    )
    for _ in range(2):
        transport = QueueTransport()
        client = NodeRpcClient(
            transport,
            event_handler=handler,
            event_state_path=state_path,
        )
        await client.start()
        await transport.incoming.put(event)
        await asyncio.wait_for(transport.acked.wait(), timeout=1)
        await client.close()
        ack = next(message for message in transport.sent if message.kind == "ack")
        assert ack.request_id == "worker-a:1"
        assert ack.payload == {"target_node_id": "worker-a"}

    assert handled == ["once"]


@pytest.mark.asyncio
async def test_failed_worker_event_is_not_acked_and_can_be_replayed(tmp_path):
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.sent: list[NodeEnvelope] = []

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    attempts = 0
    applied = asyncio.Event()

    async def handler(_message: NodeEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary apply failure")
        applied.set()

    transport = QueueTransport()
    client = NodeRpcClient(
        transport,
        event_handler=handler,
        event_state_path=tmp_path / "leader-events.json",
    )
    event = NodeEnvelope(
        kind="event",
        request_id="worker-a:1",
        sequence=1,
        payload={"node_id": "worker-a", "event_stream_id": "stream-a"},
    )
    await client.start()
    try:
        await transport.incoming.put(event)
        while attempts < 1:
            await asyncio.sleep(0)
        assert not [message for message in transport.sent if message.kind == "ack"]

        await transport.incoming.put(event)
        await asyncio.wait_for(applied.wait(), timeout=1)
        while not [message for message in transport.sent if message.kind == "ack"]:
            await asyncio.sleep(0)
    finally:
        await client.close()

    assert attempts == 2
    assert [
        message.request_id for message in transport.sent if message.kind == "ack"
    ] == ["worker-a:1"]


@pytest.mark.asyncio
async def test_graceful_close_drains_accepted_worker_event(tmp_path):
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.sent: list[NodeEnvelope] = []

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_message: NodeEnvelope) -> None:
        started.set()
        await release.wait()

    transport = QueueTransport()
    client = NodeRpcClient(
        transport,
        event_handler=handler,
        event_state_path=tmp_path / "leader-events.json",
    )
    await client.start()
    await transport.incoming.put(
        NodeEnvelope(
            kind="event",
            request_id="worker-a:1",
            sequence=1,
            payload={"node_id": "worker-a", "event_stream_id": "stream-a"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    close = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await asyncio.wait_for(close, timeout=1)

    assert [
        message.request_id for message in transport.sent if message.kind == "ack"
    ] == ["worker-a:1"]


@pytest.mark.asyncio
async def test_idle_reader_reconnects_and_delivers_health_without_rpc() -> None:
    class QueueTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope | Exception] = asyncio.Queue()
            self.closed = False

        async def send(self, _message: NodeEnvelope) -> None:
            return None

        async def receive(self) -> NodeEnvelope:
            item = await self.incoming.get()
            if isinstance(item, Exception):
                raise item
            return item

        async def close(self) -> None:
            self.closed = True

    first = QueueTransport()
    replacement = QueueTransport()
    reconnects = 0
    health_seen = asyncio.Event()

    async def reconnect() -> QueueTransport:
        nonlocal reconnects
        reconnects += 1
        return replacement

    async def handle(message: NodeEnvelope) -> None:
        if message.kind == "health" and message.payload == {"node_id": "worker-a"}:
            health_seen.set()

    client = NodeRpcClient(first, reconnect=reconnect, event_handler=handle)
    await client.start()
    try:
        await first.incoming.put(ConnectionError("idle relay closed"))
        await replacement.incoming.put(
            NodeEnvelope(kind="health", payload={"node_id": "worker-a"})
        )

        await asyncio.wait_for(health_seen.wait(), timeout=0.1)
        assert reconnects == 1
        assert first.closed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_intentional_close_does_not_reconnect() -> None:
    class BlockingTransport:
        async def send(self, _message: NodeEnvelope) -> None:
            return None

        async def receive(self) -> NodeEnvelope:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            return None

    reconnect = AsyncMock(return_value=BlockingTransport())
    client = NodeRpcClient(BlockingTransport(), reconnect=reconnect)

    await client.start()
    await client.close()
    await asyncio.sleep(0)

    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_and_reader_failure_share_one_reconnect() -> None:
    class FailedTransport:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def send(self, _message: NodeEnvelope) -> None:
            raise ConnectionError("send failed")

        async def receive(self) -> NodeEnvelope:
            await self.closed.wait()
            raise ConnectionError("reader failed")

        async def close(self) -> None:
            self.closed.set()

    class ReplyTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()

        async def send(self, message: NodeEnvelope) -> None:
            await self.incoming.put(
                NodeEnvelope(
                    kind="result",
                    request_id=message.request_id,
                    payload={"ok": True},
                )
            )

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    first = FailedTransport()
    replacement = ReplyTransport()
    reconnects = 0

    async def reconnect() -> ReplyTransport:
        nonlocal reconnects
        reconnects += 1
        await asyncio.sleep(0)
        return replacement

    client = NodeRpcClient(first, reconnect=reconnect, request_timeout=0.2)
    try:
        result = await client.request("worker-a", "health", retries=1)

        assert result == {"ok": True}
        assert reconnects == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_response_timeout_keeps_shared_transport_and_unrelated_rpc_alive() -> (
    None
):
    class MultiplexTransport:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()
            self.closed = False
            self.slow_request_id = ""

        async def send(self, message: NodeEnvelope) -> None:
            operation = str((message.payload or {}).get("operation", ""))
            if operation == "slow_restore":
                self.slow_request_id = message.request_id
                return
            await self.incoming.put(
                NodeEnvelope(
                    kind="result",
                    request_id=message.request_id,
                    payload={"ok": True, "operation": operation},
                )
            )

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

        async def close(self) -> None:
            self.closed = True

    transport = MultiplexTransport()
    reconnect = AsyncMock()
    client = NodeRpcClient(transport, reconnect=reconnect, request_timeout=0.02)
    slow = asyncio.create_task(
        client.request("worker-a", "slow_restore", retries=1, timeout=0.02)
    )
    try:
        while not transport.slow_request_id:
            await asyncio.sleep(0)
        await asyncio.sleep(0.025)
        health = await client.request("worker-a", "health", retries=0, timeout=0.1)
        await transport.incoming.put(
            NodeEnvelope(
                kind="result",
                request_id=transport.slow_request_id,
                payload={"ok": True, "restored": True},
            )
        )

        assert health == {"ok": True, "operation": "health"}
        assert await slow == {"ok": True, "restored": True}
        reconnect.assert_not_awaited()
        assert transport.closed is False
    finally:
        await client.close()
