from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.node_runtime import RemoteNodeRuntime
from ccbot.node_inbox import MAX_SESSION_FILE_BYTES
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
async def test_remote_runtime_sends_restore_contract_to_worker():
    rpc = FakeRpc()
    runtime = RemoteNodeRuntime(rpc)

    await runtime.create_session(
        "worker-a",
        "/srv/project",
        "codex",
        "Task",
        resume_session_id="rollout-42",
        source_backend="codex",
        provider_transcript_path="/srv/codex/rollout-42.jsonl",
    )

    operation, payload = rpc.calls[0]
    assert operation == "restore_session"
    assert payload["target_node_id"] == "worker-a"
    assert payload["resume_session_id"] == "rollout-42"
    assert payload["source_backend"] == "codex"
    assert payload["provider_transcript_path"] == "/srv/codex/rollout-42.jsonl"
    assert rpc.options[0] == {"retries": 0, "timeout": 135.0}


@pytest.mark.asyncio
async def test_remote_runtime_controls_and_terminates_worker_session():
    rpc = FakeRpc()
    runtime = RemoteNodeRuntime(rpc)

    await runtime.send_key("worker-a", "worker-session", "Escape")
    await runtime.capture_session("worker-a", "worker-session", with_ansi=True)
    await runtime.terminate_session("worker-a", "worker-session")
    await runtime.revoke_node("worker-a")

    assert [operation for operation, _payload in rpc.calls] == [
        "send_key",
        "capture_session",
        "terminate_session",
        "revoke_node",
    ]
    assert rpc.calls[0][1]["key"] == "Escape"
    assert rpc.calls[1][1]["with_ansi"] is True
    assert all(
        payload.get("session_id") == "worker-session"
        for _operation, payload in rpc.calls[:3]
    )
    assert all(options == {"retries": 0, "timeout": 5.0} for options in rpc.options)


@pytest.mark.asyncio
async def test_remote_runtime_streams_pinned_file_chunks_without_buffering() -> None:
    payload = b"exact-worker-bytes"

    class Rpc:
        async def request(
            self, _node: str, operation: str, request: dict, **_kwargs
        ) -> dict:
            if operation == "stat_session_file":
                return {
                    "ok": True,
                    "path": "/worker/result.zip",
                    "name": "result.zip",
                    "size": len(payload),
                    "version": f"{len(payload)}:42",
                }
            assert operation == "read_session_file"
            offset = request["offset"]
            chunk = payload[offset : offset + request["limit"]]
            return {
                "ok": True,
                "offset": offset,
                "data": base64.b64encode(chunk).decode("ascii"),
            }

    runtime = RemoteNodeRuntime(Rpc(), chunk_size=4)
    destination = io.BytesIO()

    result = await runtime.download_session_file_to(
        "worker-a",
        "routing-1",
        "/worker/result.zip",
        destination,
        expected_size=len(payload),
        expected_version=f"{len(payload)}:42",
    )

    assert destination.getvalue() == payload
    assert result["name"] == "result.zip"


@pytest.mark.asyncio
async def test_remote_runtime_rejects_changed_file_before_streaming() -> None:
    rpc = AsyncMock()
    rpc.request.return_value = {
        "ok": True,
        "path": "/worker/result.zip",
        "name": "result.zip",
        "size": 8,
        "version": "8:new",
    }
    runtime = RemoteNodeRuntime(rpc)
    destination = io.BytesIO()

    with pytest.raises(ValueError, match="changed before transfer"):
        await runtime.download_session_file_to(
            "worker-a",
            "routing-1",
            "/worker/result.zip",
            destination,
            expected_size=7,
            expected_version="7:old",
        )

    assert destination.getvalue() == b""


@pytest.mark.asyncio
async def test_remote_runtime_rejects_oversized_file_before_streaming() -> None:
    size = MAX_SESSION_FILE_BYTES + 1
    rpc = AsyncMock()
    rpc.request.return_value = {
        "ok": True,
        "path": "/worker/huge.zip",
        "name": "huge.zip",
        "size": size,
        "version": f"{size}:1",
    }
    runtime = RemoteNodeRuntime(rpc)
    destination = io.BytesIO()

    with pytest.raises(ValueError, match="exceeds transfer limit"):
        await runtime.download_session_file_to(
            "worker-a",
            "routing-1",
            "/worker/huge.zip",
            destination,
            expected_size=size,
            expected_version=f"{size}:1",
        )

    assert destination.getvalue() == b""


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
