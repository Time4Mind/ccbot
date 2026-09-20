from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ccbot.node_runtime import RemoteNodeRuntime
from ccbot.session_models import Session
from ccbot.transfer_models import SessionTransfer


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.options: list[dict[str, object]] = []

    async def request(
        self, target_node_id: str, operation: str, payload: dict, **kwargs
    ):
        payload = {"target_node_id": target_node_id, **payload}
        self.calls.append((operation, dict(payload)))
        self.options.append(dict(kwargs))
        if operation == "transfer_context_begin":
            return {"transfer_id": "worker-transfer"}
        if operation == "transfer_context_finish":
            return {
                "target_window_id": "@42",
                "target_workdir": "/srv/work",
                "target_agent_session_id": "agent-42",
                "context_error": "",
            }
        return {"ok": True}


@pytest.mark.asyncio
async def test_remote_runtime_streams_context_and_returns_prompt_delivery(tmp_path):
    context = tmp_path / "full-context.md"
    context.write_bytes(b"context-" + b"x" * 10)
    rpc = FakeRpc()
    runtime = RemoteNodeRuntime(rpc, chunk_size=5)
    source = Session(id="source", name="Task", backend="claude", node_id="local")
    transfer = SessionTransfer(
        id="transfer",
        source_session_id=source.id,
        source_node_id="local",
        target_node_id="worker-a",
        target_backend="codex",
        context_path=str(context),
    )

    result = await runtime.start_context_transfer(
        transfer=transfer,
        source=source,
        user_id=42,
        bot=SimpleNamespace(),
    )

    assert result.target_window_id == "@42"
    operations = [operation for operation, _payload in rpc.calls]
    assert operations == [
        "transfer_context_begin",
        "transfer_context_chunk",
        "transfer_context_chunk",
        "transfer_context_chunk",
        "transfer_context_chunk",
        "transfer_context_finish",
    ]
    assert all(payload["target_node_id"] == "worker-a" for _, payload in rpc.calls)

    update = SimpleNamespace(message=SimpleNamespace(text="next prompt"))
    assert await result.delivery(update, SimpleNamespace())
    assert rpc.calls[-1][0] == "send_text"
    assert rpc.calls[-1][1]["text"] == "next prompt"


@pytest.mark.asyncio
async def test_remote_runtime_uses_target_node_for_directory_and_session_operations():
    rpc = FakeRpc()
    runtime = RemoteNodeRuntime(rpc)

    directories = await runtime.list_directories("worker-a", "/srv")
    created = await runtime.create_directory("worker-a", "/srv", "project")
    session = await runtime.create_session("worker-a", "/srv/project", "claude", "Task")
    sent = await runtime.send_text("worker-a", "worker-session", "hello")

    assert directories == {"ok": True}
    assert created == {"ok": True}
    assert session == {"ok": True}
    assert sent == {"ok": True}
    assert [operation for operation, _payload in rpc.calls] == [
        "list_directories",
        "create_directory",
        "create_session",
        "send_text",
    ]
    assert rpc.calls[0][1] == {
        "target_node_id": "worker-a",
        "path": "/srv",
    }
    assert rpc.calls[1][1] == {
        "target_node_id": "worker-a",
        "path": "/srv",
        "name": "project",
    }
    assert rpc.calls[2][1] == {
        "target_node_id": "worker-a",
        "path": "/srv/project",
        "backend": "claude",
        "name": "Task",
        "startup_id": rpc.calls[2][1]["startup_id"],
    }
    assert rpc.calls[3][1] == {
        "target_node_id": "worker-a",
        "session_id": "worker-session",
        "text": "hello",
    }


@pytest.mark.asyncio
async def test_remote_runtime_controls_and_terminates_worker_session():
    rpc = FakeRpc()
    runtime = RemoteNodeRuntime(rpc)

    await runtime.send_key("worker-a", "worker-session", "Escape")
    await runtime.capture_session("worker-a", "worker-session")
    await runtime.terminate_session("worker-a", "worker-session")
    await runtime.revoke_node("worker-a")

    assert [operation for operation, _payload in rpc.calls] == [
        "send_key",
        "capture_session",
        "terminate_session",
        "revoke_node",
    ]
    assert rpc.calls[0][1]["key"] == "Escape"
    assert all(
        payload.get("session_id") == "worker-session"
        for _operation, payload in rpc.calls[:3]
    )
    assert all(options == {"retries": 0, "timeout": 5.0} for options in rpc.options)


@pytest.mark.asyncio
async def test_cancelling_remote_creation_requests_worker_cleanup() -> None:
    started = asyncio.Event()
    calls: list[tuple[str, dict[str, object]]] = []

    class Rpc:
        async def request(
            self,
            target_node_id: str,
            operation: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append((operation, dict(payload)))
            if operation == "create_session":
                started.set()
                await asyncio.Event().wait()
            return {"ok": True}

    runtime = RemoteNodeRuntime(Rpc())
    task = asyncio.create_task(
        runtime.create_session("worker-a", "/srv/project", "codex", "Task")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [operation for operation, _payload in calls] == [
        "create_session",
        "cancel_session_start",
    ]
    assert calls[1][1]["startup_id"] == calls[0][1]["startup_id"]
