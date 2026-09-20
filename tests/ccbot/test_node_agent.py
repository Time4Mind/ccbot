from __future__ import annotations

import asyncio
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

from ccbot.node_agent import NodeAgent, NodeCredentialStore, TmuxWorkerExecutor
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


def test_reconnect_credential_is_private_and_reloadable(tmp_path):
    path = tmp_path / "worker" / "credential.json"
    store = NodeCredentialStore(path)

    store.save(
        node_id="worker-a",
        leader_id="leader",
        relay_url="tls://relay:8765",
        secret="reconnect-secret",
    )

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert (
        store.load(
            node_id="worker-a",
            leader_id="leader",
            relay_url="tls://relay:8765",
        )
        == "reconnect-secret"
    )
    assert (
        store.load(node_id="other", leader_id="leader", relay_url="tls://relay:8765")
        == ""
    )


@pytest.mark.asyncio
async def test_tmux_executor_recovers_managed_session_after_process_restart(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    async def fake_tmux(*args: str):
        assert args[0] == "list-windows"
        return 0, "@7\tagent-7\tcodex\n@8\t\t\n", ""

    delivered = []

    async def fake_send(window_id: str, text: str, *, backend: str):
        delivered.append((window_id, text, backend))
        return True

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(
        "ccbot.node_agent.tmux_input_transport.send_literal_chunked", fake_send
    )

    result = await executor.send_text(session_id="agent-7", text="continue")

    assert result == {"ok": True, "error": ""}
    assert delivered == [("@7", "continue", "codex")]


@pytest.mark.asyncio
async def test_new_worker_session_is_marked_for_restart_recovery(tmp_path, monkeypatch):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())

    result = await executor.create_session(
        path=str(tmp_path), backend="claude", name="Task"
    )

    session_id = result["target_agent_session_id"]
    assert ("set-option", "-w", "-t", "@9", "@ccbot_session_id", session_id) in calls
    assert ("set-option", "-w", "-t", "@9", "@ccbot_backend", "claude") in calls


@pytest.mark.asyncio
async def test_worker_emits_assistant_transcript_events_for_remote_card(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "session_map.json").write_text(
        json.dumps(
            {
                "ccbot-worker:@9": {
                    "session_id": "provider-session",
                    "transcript_path": str(transcript),
                }
            }
        ),
        encoding="utf-8",
    )
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    async def fake_tmux(*args: str):
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())
    monkeypatch.setattr(
        "ccbot.node_agent.tmux_input_transport.send_literal_chunked",
        AsyncMock(return_value=True),
    )
    created = await executor.create_session(
        path=str(tmp_path), backend="claude", name="Task"
    )
    session_id = created["target_agent_session_id"]
    await executor.send_text(session_id=session_id, text="hello")
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "remote answer"}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )

    events = await executor.poll_events()

    assert events == [
        {
            "event_type": "session_message",
            "session_id": session_id,
            "text": "remote answer",
            "content_type": "text",
            "tool_use_id": None,
            "tool_name": None,
            "stop_reason": "end_turn",
            "timestamp": "",
            "is_error": False,
            "api_error": "",
        }
    ]


@pytest.mark.asyncio
async def test_worker_reads_only_appended_transcript_bytes(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("old transcript data\n", encoding="utf-8")
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    session = SimpleNamespace(
        session_id="agent-1",
        window_id="@1",
        backend="claude",
        transcript_path=transcript,
        transcript_offset=transcript.stat().st_size,
        pending_tools={},
    )
    executor._sessions[session.session_id] = session

    def reject_full_file_read(_path):
        raise AssertionError("polling must not reread the complete transcript")

    monkeypatch.setattr(Path, "read_bytes", reject_full_file_read)
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "new answer"}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )

    events = await executor.poll_events()

    assert [event["text"] for event in events] == ["new answer"]


@pytest.mark.asyncio
async def test_worker_rejects_session_creation_when_capacity_is_full(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(workdir=tmp_path, max_sessions=1)
    executor._sessions["existing"] = SimpleNamespace(window_id="@1")

    async def fake_tmux(*args: str):
        if args[0] == "list-windows":
            return 0, "@1\texisting\tcodex\n", ""
        raise AssertionError("capacity rejection must happen before creating a window")

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    with pytest.raises(RuntimeError, match="capacity"):
        await executor.create_session(
            path=str(tmp_path), backend="codex", name="overflow"
        )


@pytest.mark.asyncio
async def test_worker_stops_polling_sessions_whose_tmux_windows_disappeared(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(workdir=tmp_path, reconcile_interval=0)
    executor._sessions["stale"] = SimpleNamespace(
        session_id="stale",
        window_id="@1",
        backend="codex",
        transcript_path=tmp_path / "stale.jsonl",
        transcript_offset=0,
        pending_tools={},
    )

    async def fake_tmux(*args: str):
        assert args[0] == "list-windows"
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    assert await executor.poll_events() == []
    assert executor.capacity_snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_control_command_is_not_blocked_by_slow_session_creation(tmp_path):
    class QueueTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()

        async def receive(self) -> NodeEnvelope:
            return await self.incoming.get()

    class BlockingExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.create_started = asyncio.Event()
            self.release_create = asyncio.Event()
            self.key_sent = asyncio.Event()

        async def create_session(self, **_kwargs):
            self.create_started.set()
            await self.release_create.wait()
            return {"ok": True}

        async def send_key(self, *, session_id: str, key: str):
            self.key_sent.set()
            return {"ok": True, "session_id": session_id, "key": key}

    transport = QueueTransport()
    executor = BlockingExecutor()
    agent = NodeAgent(transport, executor, context_dir=tmp_path)
    task = asyncio.create_task(agent.run())
    await transport.incoming.put(
        NodeEnvelope(
            kind="command",
            request_id="create",
            payload={
                "operation": "create_session",
                "path": str(tmp_path),
                "backend": "codex",
                "name": "slow",
            },
        )
    )
    await asyncio.wait_for(executor.create_started.wait(), timeout=1)
    await transport.incoming.put(
        NodeEnvelope(
            kind="command",
            request_id="escape",
            payload={"operation": "send_key", "session_id": "s1", "key": "Escape"},
        )
    )

    await asyncio.wait_for(executor.key_sent.wait(), timeout=1)
    executor.release_create.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
