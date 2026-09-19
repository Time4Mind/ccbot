from __future__ import annotations

import asyncio

import pytest

from ccbot.node_transport import (
    NodeEnvelope,
    RelayServer,
    StreamNodeTransport,
    connect_relay,
)
from ccbot.node_pairing import create_pairing_invitation


@pytest.mark.asyncio
async def test_relay_routes_leader_commands_and_worker_results() -> None:
    server = RelayServer(
        credentials={"leader": "leader-secret", "worker-a": "worker-secret"},
        leader_id="leader",
    )
    await server.start("127.0.0.1", 0)
    leader = await connect_relay(
        "127.0.0.1",
        server.port,
        node_id="leader",
        role="leader",
        secret="leader-secret",
        leader_id="leader",
    )
    worker = await connect_relay(
        "127.0.0.1",
        server.port,
        node_id="worker-a",
        role="worker",
        secret="worker-secret",
        leader_id="leader",
    )

    await leader.send(
        NodeEnvelope(
            kind="command",
            request_id="r1",
            payload={"target_node_id": "worker-a", "operation": "health"},
        )
    )
    received = await asyncio.wait_for(worker.receive(), timeout=1)
    assert received.request_id == "r1"
    assert received.payload["operation"] == "health"

    await worker.send(
        NodeEnvelope(
            kind="result",
            request_id="r1",
            payload={"ok": True, "target_node_id": "leader"},
        )
    )
    result = await asyncio.wait_for(leader.receive(), timeout=1)
    assert result.request_id == "r1"
    assert result.payload["ok"] is True

    await leader.close()
    await worker.close()
    await server.close()


@pytest.mark.asyncio
async def test_relay_rejects_invalid_credentials() -> None:
    server = RelayServer(credentials={"leader": "secret"}, leader_id="leader")
    await server.start("127.0.0.1", 0)

    with pytest.raises(PermissionError, match="relay authentication failed"):
        await connect_relay(
            "127.0.0.1",
            server.port,
            node_id="leader",
            role="leader",
            secret="wrong",
            leader_id="leader",
        )

    await server.close()


@pytest.mark.asyncio
async def test_relay_accepts_signed_bootstrap_worker_without_static_credential() -> (
    None
):
    server = RelayServer(credentials={"leader": "leader-secret"}, leader_id="leader")
    await server.start("127.0.0.1", 0)
    invitation = create_pairing_invitation(
        relay_url=f"127.0.0.1:{server.port}",
        leader_id="leader",
        signing_secret="leader-secret",
        ttl=600,
    )
    leader = await connect_relay(
        "127.0.0.1",
        server.port,
        node_id="leader",
        role="leader",
        secret="leader-secret",
        leader_id="leader",
    )
    worker = await connect_relay(
        "127.0.0.1",
        server.port,
        node_id="worker-from-hostname",
        role="worker",
        secret=invitation.secret,
        leader_id="leader",
        pairing_nonce=invitation.nonce,
        pairing_expires=invitation.expires_at,
    )

    await leader.send(
        NodeEnvelope(
            kind="command",
            request_id="bootstrap-1",
            payload={
                "target_node_id": "worker-from-hostname",
                "operation": "health",
            },
        )
    )
    received = await asyncio.wait_for(worker.receive(), timeout=1)
    assert received.payload["operation"] == "health"

    await leader.close()
    await worker.close()
    await server.close()


@pytest.mark.asyncio
async def test_stream_transport_round_trips_json_lines() -> None:
    received: list[NodeEnvelope] = []

    async def handler(reader, writer):
        transport = StreamNodeTransport(reader, writer)
        received.append(await transport.receive())
        await transport.send(NodeEnvelope(kind="ack", request_id="r1"))
        await transport.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = await StreamNodeTransport.connect("127.0.0.1", port)
    await transport.send(NodeEnvelope(kind="command", request_id="r1"))
    response = await asyncio.wait_for(transport.receive(), timeout=1)

    assert response.kind == "ack"
    assert received == [NodeEnvelope(kind="command", request_id="r1", payload={})]
    await transport.close()
    server.close()
    await server.wait_closed()
