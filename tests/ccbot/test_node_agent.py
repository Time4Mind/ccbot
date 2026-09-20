from __future__ import annotations

import pytest

from ccbot.node_agent import NodeAgent
from ccbot.node_transport import NodeEnvelope


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[NodeEnvelope] = []

    async def send(self, message: NodeEnvelope) -> None:
        self.sent.append(message)

    async def receive(self) -> NodeEnvelope:
        raise AssertionError("receive is not used in this unit test")

    async def close(self) -> None:
        return None


class FakeExecutor:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    async def start_context_session(self, **kwargs):
        self.started.append(kwargs)
        return {
            "target_window_id": "@7",
            "target_workdir": "/worker",
            "target_agent_session_id": "agent-7",
            "context_error": "",
        }

    async def send_text(self, *, session_id: str, text: str):
        self.sent.append((session_id, text))
        return {"ok": True}

    async def list_directories(self, *, path: str):
        return {"ok": True, "path": path or "/worker", "directories": ["project"]}

    async def create_directory(self, *, path: str, name: str):
        return {"ok": True, "path": f"{path}/{name}", "directories": []}

    async def create_session(self, *, path: str, backend: str, name: str):
        return {
            "ok": True,
            "target_window_id": "@8",
            "target_workdir": path,
            "target_agent_session_id": f"agent-{name}",
            "backend": backend,
        }


@pytest.mark.asyncio
async def test_node_agent_accepts_context_chunks_and_deduplicates_commands(tmp_path):
    transport = FakeTransport()
    executor = FakeExecutor()
    agent = NodeAgent(transport, executor, context_dir=tmp_path)

    content = b"hello worker"
    import hashlib
    import base64

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="begin",
            payload={
                "operation": "transfer_context_begin",
                "total_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "target_backend": "codex",
                "source_name": "Task",
            },
        )
    )
    worker_transfer_id = transport.sent[-1].payload["transfer_id"]
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="chunk",
            payload={
                "operation": "transfer_context_chunk",
                "transfer_id": worker_transfer_id,
                "chunk_index": 0,
                "data": base64.b64encode(content).decode("ascii"),
            },
        )
    )
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="finish",
            payload={
                "operation": "transfer_context_finish",
                "transfer_id": worker_transfer_id,
            },
        )
    )
    result_count = len(
        [message for message in transport.sent if message.kind == "result"]
    )
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="finish",
            payload={
                "operation": "transfer_context_finish",
                "transfer_id": worker_transfer_id,
            },
        )
    )

    assert len(executor.started) == 1
    assert (
        len([message for message in transport.sent if message.kind == "result"])
        == result_count + 1
    )
    assert transport.sent[-1].payload["target_agent_session_id"] == "agent-7"


@pytest.mark.asyncio
async def test_node_agent_routes_send_text_to_executor(tmp_path):
    transport = FakeTransport()
    executor = FakeExecutor()
    agent = NodeAgent(transport, executor, context_dir=tmp_path)

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="send",
            payload={"operation": "send_text", "session_id": "agent-7", "text": "next"},
        )
    )

    assert executor.sent == [("agent-7", "next")]
    assert transport.sent[-1].kind == "result"
    assert transport.sent[-1].payload["ok"] is True


@pytest.mark.asyncio
async def test_node_agent_routes_directory_and_new_session_commands(tmp_path):
    transport = FakeTransport()
    executor = FakeExecutor()
    agent = NodeAgent(transport, executor, context_dir=tmp_path)

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="dirs",
            payload={"operation": "list_directories", "path": "/worker"},
        )
    )
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="mkdir",
            payload={
                "operation": "create_directory",
                "path": "/worker",
                "name": "project",
            },
        )
    )
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="session",
            payload={
                "operation": "create_session",
                "path": "/worker/project",
                "backend": "claude",
                "name": "Task",
            },
        )
    )

    results = [
        message.payload for message in transport.sent if message.kind == "result"
    ]
    assert results[-3:] == [
        {"ok": True, "path": "/worker", "directories": ["project"]},
        {"ok": True, "path": "/worker/project", "directories": []},
        {
            "ok": True,
            "target_window_id": "@8",
            "target_workdir": "/worker/project",
            "target_agent_session_id": "agent-Task",
            "backend": "claude",
        },
    ]
