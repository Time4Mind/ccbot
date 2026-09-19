from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import node_runtime
from ccbot.session import session_manager
from ccbot.node_transport import NodeEnvelope


@pytest.mark.asyncio
async def test_worker_health_auto_registers_remote_node_and_runtime(monkeypatch):
    rpc = SimpleNamespace(close=AsyncMock())
    captured: dict[str, object] = {}

    async def fake_connect_leader_rpc(**kwargs):
        captured.update(kwargs)
        return rpc

    registered_nodes = []
    registered_runtimes = []
    monkeypatch.setattr(node_runtime, "connect_leader_rpc", fake_connect_leader_rpc)
    monkeypatch.setattr(
        session_manager,
        "get_node",
        lambda _node_id: None,
        raising=False,
    )
    monkeypatch.setattr(
        session_manager,
        "register_node",
        registered_nodes.append,
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
            },
        )
    )

    assert registered_nodes[0].id == "worker-a"
    assert registered_nodes[0].state == "ready"
    assert registered_runtimes == ["worker-a"]
    await node_runtime.shutdown_remote_runtimes()
