from __future__ import annotations

import asyncio
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

from ccbot.node_agent import NodeAgent, NodeCredentialStore, TmuxWorkerExecutor
from ccbot import node_backend_readiness
from ccbot.node_transport import NodeEnvelope
from ccbot.config import config
from ccbot.transcript_parser import ParsedEntry, TranscriptParser


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[NodeEnvelope] = []

    async def send(self, message: NodeEnvelope) -> None:
        self.sent.append(message)

    async def receive(self) -> NodeEnvelope:
        raise AssertionError("receive is not used in this unit test")

    async def close(self) -> None:
        return None


class QueueTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.incoming: asyncio.Queue[NodeEnvelope] = asyncio.Queue()

    async def receive(self) -> NodeEnvelope:
        return await self.incoming.get()


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

    async def create_session(
        self, *, path: str, backend: str, name: str, startup_id: str = ""
    ):
        return {
            "ok": True,
            "target_window_id": "@8",
            "target_workdir": path,
            "target_agent_session_id": f"agent-{name}",
            "backend": backend,
        }

    async def cancel_session_start(self, *, startup_id: str):
        return {"ok": bool(startup_id)}


@pytest.mark.asyncio
async def test_slow_worker_startup_does_not_block_other_worker_commands(tmp_path):
    transport = QueueTransport()
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()

    class Executor(FakeExecutor):
        async def create_session(self, **_kwargs):
            startup_entered.set()
            await release_startup.wait()
            return {"ok": True}

    agent = NodeAgent(transport, Executor(), context_dir=tmp_path)
    run_task = asyncio.create_task(agent.run())
    await transport.incoming.put(
        NodeEnvelope(
            kind="command",
            request_id="start",
            payload={
                "operation": "create_session",
                "path": "/worker",
                "backend": "codex",
                "name": "slow",
                "startup_id": "startup-1",
            },
        )
    )
    await startup_entered.wait()
    await transport.incoming.put(
        NodeEnvelope(
            kind="command",
            request_id="dirs",
            payload={"operation": "list_directories", "path": "/worker"},
        )
    )

    for _ in range(20):
        if any(
            message.kind == "result" and message.request_id == "dirs"
            for message in transport.sent
        ):
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("ordinary worker command remained blocked behind startup")

    release_startup.set()
    for _ in range(20):
        if any(
            message.kind == "result" and message.request_id == "start"
            for message in transport.sent
        ):
            break
        await asyncio.sleep(0)
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


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
async def test_node_agent_rejects_create_for_unadvertised_backend(tmp_path):
    transport = FakeTransport()
    executor = FakeExecutor()
    agent = NodeAgent(transport, executor, context_dir=tmp_path, backends=("codex",))

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="wrong-backend",
            payload={
                "operation": "create_session",
                "path": "/worker",
                "backend": "claude",
                "name": "must-not-start",
            },
        )
    )

    assert transport.sent[-1].payload["ok"] is False
    assert "unavailable" in transport.sent[-1].payload["error"].lower()


@pytest.mark.asyncio
async def test_worker_advertises_only_cli_with_working_auth(tmp_path, monkeypatch):
    class Process:
        def __init__(self, return_code: int) -> None:
            self.return_code = return_code

        async def wait(self):
            return self.return_code

        def kill(self):
            return None

    async def create_process(executable, *args, **_kwargs):
        assert args[-2:] in (("auth", "status"), ("login", "status"))
        return Process(0 if executable.endswith("codex") else 1)

    monkeypatch.setattr(
        node_backend_readiness.shutil,
        "which",
        lambda name: f"/usr/local/bin/{name}" if name in ("claude", "codex") else None,
    )
    monkeypatch.setattr(
        node_backend_readiness.asyncio, "create_subprocess_exec", create_process
    )
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    ready = await executor.ready_backends(("claude", "codex"), max_age=0.0)

    assert ready == ("codex",)


@pytest.mark.asyncio
async def test_node_agent_never_returns_an_empty_worker_error(tmp_path):
    class FailingExecutor(FakeExecutor):
        async def send_text(self, *, session_id: str, text: str):
            raise RuntimeError()

    transport = FakeTransport()
    agent = NodeAgent(transport, FailingExecutor(), context_dir=tmp_path)

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="send-failed",
            payload={"operation": "send_text", "session_id": "agent-7", "text": "x"},
        )
    )

    assert transport.sent[-1].payload == {"ok": False, "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_worker_health_reports_exact_runtime_revision(tmp_path):
    transport = FakeTransport()
    agent = NodeAgent(
        transport,
        FakeExecutor(),
        context_dir=tmp_path,
        runtime_revision="a" * 40,
    )

    await agent._send_health()

    assert transport.sent[-1].payload["ccbot_version"] == "a" * 40


@pytest.mark.asyncio
async def test_worker_health_advertises_configured_same_user_ssh_access(tmp_path):
    transport = FakeTransport()
    agent = NodeAgent(
        transport,
        FakeExecutor(),
        context_dir=tmp_path,
        ssh_access={
            "host": "127.0.0.1",
            "user": "artem",
            "port": 22041,
            "proxy_jump": "bastion",
        },
    )

    await agent._send_health()

    assert transport.sent[-1].payload["ssh"] == {
        "host": "127.0.0.1",
        "user": "artem",
        "port": 22041,
        "proxy_jump": "bastion",
    }


@pytest.mark.asyncio
async def test_successful_runtime_update_restarts_only_after_result(tmp_path):
    transport = FakeTransport()
    updater = SimpleNamespace(
        update=AsyncMock(
            return_value={
                "ok": True,
                "revision": "b" * 40,
                "previous_revision": "a" * 40,
                "restart_required": True,
            }
        )
    )
    restart = AsyncMock()
    agent = NodeAgent(
        transport,
        FakeExecutor(),
        context_dir=tmp_path,
        runtime_updater=updater,
        restart_callback=restart,
    )

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="update-1",
            payload={"operation": "update_runtime", "revision": "b" * 40},
        )
    )
    await asyncio.sleep(0)

    updater.update.assert_awaited_once_with("b" * 40)
    assert [message.kind for message in transport.sent] == ["ack", "result"]
    assert transport.sent[-1].payload["restart_required"] is True
    restart.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_runtime_update_keeps_worker_running(tmp_path):
    transport = FakeTransport()
    updater = SimpleNamespace(
        update=AsyncMock(
            side_effect=RuntimeError("worker checkout has tracked changes")
        )
    )
    restart = AsyncMock()
    agent = NodeAgent(
        transport,
        FakeExecutor(),
        context_dir=tmp_path,
        runtime_updater=updater,
        restart_callback=restart,
    )

    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="update-failed",
            payload={"operation": "update_runtime", "revision": "b" * 40},
        )
    )
    await asyncio.sleep(0)

    assert transport.sent[-1].kind == "result"
    assert transport.sent[-1].payload == {
        "ok": False,
        "error": "worker checkout has tracked changes",
    }
    restart.assert_not_awaited()


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
        "ccbot.node_worker.tmux_input_transport.send_literal_chunked", fake_send
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
        "ccbot.node_worker.tmux_input_transport.send_literal_chunked",
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

    assert events[0] == {
        "event_type": "session_binding",
        "session_id": session_id,
        "provider_session_id": "provider-session",
        "transcript_path": str(transcript),
    }
    assert len(events) == 2
    message = events[1]
    assert message == {
        "event_type": "session_message",
        "session_id": session_id,
        "role": "assistant",
        "text": "remote answer",
        "content_type": "text",
        "tool_use_id": None,
        "tool_name": None,
        "image_data": [],
        "stop_reason": "end_turn",
        "timestamp": None,
        "is_error": False,
        "api_error": "",
        "_transcript_path": str(transcript),
        "_transcript_offset": transcript.stat().st_size,
    }


@pytest.mark.asyncio
async def test_worker_resumes_existing_codex_rollout_on_worker(tmp_path, monkeypatch):
    transcript = tmp_path / "old" / "2026" / "09" / "21" / "rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "rollout-42"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "codex_sessions_path", tmp_path / "active" / "sessions")
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())
    monkeypatch.setattr(
        executor,
        "_restore_transcript_path",
        lambda *_args: transcript,
    )

    result = await executor.create_session(
        path=str(tmp_path),
        backend="codex",
        name="Restored",
        resume_session_id="rollout-42",
        source_backend="codex",
    )

    assert result["target_agent_session_id"] != "rollout-42"
    assert result["provider_session_id"] == "rollout-42"
    assert any(
        call[:3] == ("send-keys", "-t", "@9")
        and call[3].endswith("codex resume rollout-42")
        and call[4] == "C-m"
        for call in calls
    )


@pytest.mark.asyncio
async def test_worker_rejects_missing_restore_rollout_before_window_creation(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(
        "ccbot.codex_session_io.build_session_file_path",
        lambda *_args: None,
    )

    with pytest.raises(ValueError, match="Codex rollout not found on worker"):
        await executor.create_session(
            path=str(tmp_path),
            backend="codex",
            name="Restored",
            resume_session_id="missing",
            source_backend="codex",
        )

    assert not any(call[0] == "new-window" for call in calls)


@pytest.mark.asyncio
async def test_worker_emits_queued_user_rows_before_remote_answer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "rollout.jsonl"
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
    await executor.create_session(path=str(tmp_path), backend="codex", name="Task")
    rows = [
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first prompt"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "second prompt"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "answer",
                "phase": "final_answer",
            },
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    events = await executor.poll_events()

    assert [
        (event["role"], event["text"])
        for event in events
        if event["event_type"] == "session_message"
    ] == [
        ("user", "first prompt"),
        ("user", "second prompt"),
        ("assistant", "answer"),
    ]


@pytest.mark.asyncio
async def test_remote_codex_delayed_binding_emits_both_turns_once(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CCBOT_DIR", str(state_dir))
    transcript = tmp_path / "rollout.jsonl"
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    async def fake_tmux(*args: str):
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())
    monkeypatch.setattr(
        "ccbot.node_worker.tmux_input_transport.send_literal_chunked",
        AsyncMock(return_value=True),
    )
    created = await executor.create_session(
        path=str(tmp_path), backend="codex", name="Task"
    )
    session_id = created["target_agent_session_id"]

    # Codex publishes session_map only after accepting the first prompt.
    await executor.send_text(session_id=session_id, text="first")
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-21T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "first answer",
                    "phase": "final_answer",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
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

    first_events = await executor.poll_events()
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-09-21T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "second answer",
                        "phase": "final_answer",
                    },
                }
            )
            + "\n"
        )
    second_events = await executor.poll_events()

    assert first_events[0] == {
        "event_type": "session_binding",
        "session_id": session_id,
        "provider_session_id": "provider-session",
        "transcript_path": str(transcript),
    }
    assert [event["text"] for event in first_events[1:]] == ["first answer"]
    assert [event["text"] for event in second_events] == ["second answer"]
    assert await executor.poll_events() == []


@pytest.mark.asyncio
async def test_worker_republishes_provider_binding_after_restart(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CCBOT_DIR", str(state_dir))
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")
    (state_dir / "session_map.json").write_text(
        json.dumps(
            {
                "ccbot-worker:@9": {
                    "session_id": "provider-01",
                    "transcript_path": str(transcript),
                }
            }
        ),
        encoding="utf-8",
    )
    executor = TmuxWorkerExecutor(workdir=tmp_path, reconcile_interval=0)

    async def fake_tmux(*args: str):
        if args[0] == "list-windows":
            return 0, "@9\trouting-42\tcodex\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    events = await executor.poll_events()

    assert events == [
        {
            "event_type": "session_binding",
            "session_id": "routing-42",
            "provider_session_id": "provider-01",
            "transcript_path": str(transcript),
        }
    ]


@pytest.mark.asyncio
async def test_legacy_restore_rejects_ambiguous_rollouts_in_same_workdir(
    tmp_path, monkeypatch
):
    sessions_root = tmp_path / "sessions"
    day = sessions_root / "2026" / "09" / "21"
    day.mkdir(parents=True)
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.setattr(config, "codex_sessions_path", sessions_root)
    for index in (1, 2):
        (day / f"rollout-{index}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": f"provider-{index}", "cwd": str(workdir)},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "prompt"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    result = await executor.resolve_provider_session(
        path=str(workdir), backend="codex", session_id="legacy-routing"
    )

    assert result["ok"] is False
    assert result["error_code"] == "ambiguous_transcript"


@pytest.mark.asyncio
async def test_event_pump_retries_after_transient_send_failure(tmp_path):
    delivered = asyncio.Event()

    class FlakyEventTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False
            self.wait_forever = asyncio.Event()

        async def send(self, message: NodeEnvelope) -> None:
            if message.kind == "event" and not self.failed_once:
                self.failed_once = True
                raise ConnectionError("relay reset")
            await super().send(message)
            if message.kind == "event":
                delivered.set()

        async def receive(self) -> NodeEnvelope:
            await self.wait_forever.wait()
            raise AssertionError("unreachable")

    class EventExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.poll_count = 0

        async def poll_events(self):
            self.poll_count += 1
            if self.poll_count == 1:
                return [
                    {
                        "event_type": "session_message",
                        "session_id": "agent-7",
                        "text": "survives relay reset",
                    }
                ]
            return []

    transport = FlakyEventTransport()
    agent = NodeAgent(
        transport,
        EventExecutor(),
        context_dir=tmp_path,
        node_id="worker-a",
        backends=("codex",),
    )
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1.5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    events = [message for message in transport.sent if message.kind == "event"]
    assert len(events) == 1
    assert events[0].payload["text"] == "survives relay reset"
    health = [message for message in transport.sent if message.kind == "health"]
    assert any(
        message.payload["state"] == "online"
        and message.payload["capabilities"]["event_stream"] is False
        for message in health
    )
    assert health[-1].payload["state"] == "ready"
    assert health[-1].payload["capabilities"]["event_stream"] is True


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
async def test_worker_emits_complete_row_before_partial_tail(tmp_path):
    transcript = tmp_path / "session.jsonl"
    complete = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "complete answer"}],
                "stop_reason": "end_turn",
            },
        }
    )
    transcript.write_bytes((complete + "\n" + '{"type":').encode())
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    executor._sessions["agent-1"] = SimpleNamespace(
        session_id="agent-1",
        window_id="@1",
        backend="claude",
        transcript_path=transcript,
        transcript_offset=0,
        pending_tools={},
        provider_session_id="provider-1",
        binding_announced=True,
    )

    events = await executor.poll_events()

    assert [event["text"] for event in events] == ["complete answer"]
    assert executor._sessions["agent-1"].transcript_offset == len(
        (complete + "\n").encode()
    )


@pytest.mark.asyncio
async def test_worker_bounds_oversized_live_image_event(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    executor._sessions["agent-1"] = SimpleNamespace(
        session_id="agent-1",
        window_id="@1",
        backend="claude",
        transcript_path=transcript,
        transcript_offset=0,
        pending_tools={},
        provider_session_id="provider-1",
        binding_announced=True,
    )
    oversized_image = b"x" * (2 * 1024 * 1024)
    monkeypatch.setattr(
        TranscriptParser,
        "parse_entries",
        lambda *_args, **_kwargs: (
            [
                ParsedEntry(
                    role="assistant",
                    text="",
                    content_type="tool_result",
                    image_data=[("image/png", oversized_image)],
                )
            ],
            {},
        ),
    )

    events = await executor.poll_events()

    assert len(events) == 1
    assert events[0]["image_data"] == []
    assert events[0]["image_truncated"] is True
    assert events[0]["text"] == "[Image omitted: live event exceeds 2 MiB limit]"
    assert len(json.dumps(events[0], ensure_ascii=False).encode()) <= 2 * 1024 * 1024


@pytest.mark.asyncio
async def test_restored_worker_session_does_not_publish_history_as_live(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CCBOT_DIR", str(state_dir))
    transcript = tmp_path / "existing.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "historical answer"}],
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "session_map.json").write_text(
        json.dumps(
            {
                "ccbot-worker:@9": {
                    "session_id": "provider-old",
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
    monkeypatch.setattr(executor, "_restore_transcript_path", lambda *_args: transcript)
    created = await executor.create_session(
        path=str(tmp_path),
        backend="claude",
        name="Restored",
        resume_session_id="provider-old",
        source_backend="claude",
    )

    events = await executor.poll_events()

    assert created["provider_session_id"] == "provider-old"
    assert [event for event in events if event["event_type"] == "session_message"] == []


@pytest.mark.asyncio
async def test_worker_remote_screenshot_capture_preserves_ansi(tmp_path, monkeypatch):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    executor._sessions["agent-1"] = SimpleNamespace(
        session_id="agent-1", window_id="@17", backend="codex"
    )
    run_tmux = AsyncMock(return_value=(0, "\x1b[31mred\x1b[0m", ""))
    monkeypatch.setattr(executor, "_run_tmux", run_tmux)

    result = await executor.capture_session(session_id="agent-1", with_ansi=True)

    assert result["pane"] == "\x1b[31mred\x1b[0m"
    run_tmux.assert_awaited_once_with("capture-pane", "-p", "-e", "-t", "@17")


@pytest.mark.asyncio
async def test_worker_accepts_more_than_eight_sessions(tmp_path, monkeypatch):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    for index in range(8):
        executor._sessions[f"existing-{index}"] = SimpleNamespace(
            session_id=f"existing-{index}",
            window_id=f"@{index + 1}",
            backend="codex",
        )
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            rows = [f"@{index + 1}\texisting-{index}\tcodex" for index in range(8)]
            return 0, "\n".join(rows) + "\n", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())

    result = await executor.create_session(
        path=str(tmp_path), backend="codex", name="ninth"
    )

    assert result["target_window_id"] == "@9"
    assert any(call[0] == "new-window" for call in calls)


@pytest.mark.asyncio
async def test_worker_cleans_up_window_when_startup_fails(tmp_path, monkeypatch):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(
        executor, "_wait_ready", AsyncMock(side_effect=TimeoutError("not ready"))
    )

    with pytest.raises(TimeoutError, match="not ready"):
        await executor.create_session(
            path=str(tmp_path), backend="codex", name="broken"
        )

    assert ("kill-window", "-t", "@9") in calls


@pytest.mark.asyncio
async def test_worker_cleans_up_window_when_startup_is_cancelled(tmp_path, monkeypatch):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []
    startup_entered = asyncio.Event()

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        return 0, "", ""

    async def wait_forever(*_args, **_kwargs):
        startup_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", wait_forever)

    task = asyncio.create_task(
        executor.create_session(path=str(tmp_path), backend="codex", name="cancelled")
    )
    await asyncio.wait_for(startup_entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("kill-window", "-t", "@9") in calls


@pytest.mark.asyncio
async def test_worker_control_command_cancels_inflight_session_start(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    calls: list[tuple[str, ...]] = []
    startup_entered = asyncio.Event()
    window_killed = asyncio.Event()

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        if args[0] == "kill-window":
            window_killed.set()
        return 0, "", ""

    async def wait_forever(*_args, **_kwargs):
        startup_entered.set()
        await window_killed.wait()

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", wait_forever)

    task = asyncio.create_task(
        executor.create_session(
            path=str(tmp_path),
            backend="codex",
            name="cancelled",
            startup_id="startup-1",
        )
    )
    await startup_entered.wait()
    result = await executor.cancel_session_start(startup_id="startup-1")
    with pytest.raises(RuntimeError, match="cancelled"):
        await task

    assert result == {"ok": True}
    assert ("kill-window", "-t", "@9") in calls
    assert executor.capacity_snapshot()["active_sessions"] == 0


@pytest.mark.asyncio
async def test_worker_installs_codex_update_and_relaunches_exact_command(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(
        workdir=tmp_path,
        codex_flags="--no-alt-screen",
        ready_timeout=1,
        codex_poll_interval=0.001,
        codex_ready_settle_time=0.02,
    )
    screens = [
        "OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project",
        "OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project",
        (
            "Update available! 0.147.0 -> 0.151.0\n"
            "› 1. Update now\n  2. Skip\n  3. Skip until next version\n"
            "Press enter to continue"
        ),
        "Installing update",
        "shell",
    ]
    processes = ["codex", "codex", "codex", "npm", "zsh"]
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        if args[0] == "capture-pane":
            pane = (
                screens.pop(0)
                if screens
                else ("OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project")
            )
            return 0, pane, ""
        if args[0] == "display-message":
            return 0, processes.pop(0) if processes else "codex", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    result = await executor.create_session(
        path=str(tmp_path), backend="codex", name="updated"
    )

    command = "codex --no-alt-screen"
    command_sends = [
        call
        for call in calls
        if call[:3] == ("send-keys", "-t", "@9")
        and call[3].endswith(command)
        and call[4] == "C-m"
    ]
    assert result["target_window_id"] == "@9"
    assert len(command_sends) == 2
    assert command_sends[0] == command_sends[1]
    assert ("send-keys", "-t", "@9", "C-m") in calls


@pytest.mark.asyncio
async def test_worker_handles_codex_hooks_review_before_returning_ready(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(
        workdir=tmp_path,
        ready_timeout=1,
        codex_poll_interval=0,
        codex_ready_settle_time=0,
    )
    screens = iter(
        [
            "Hooks need review\n"
            "2 hooks are new or changed.\n"
            "1. Review hooks\n"
            "2. Trust all and continue\n"
            "3. Continue without trusting (hooks won't run)",
            "OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project",
        ]
    )
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        if args[0] == "capture-pane":
            return 0, next(screens), ""
        if args[0] == "display-message":
            return 0, "codex", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    result = await executor.create_session(
        path=str(tmp_path), backend="codex", name="hooks"
    )

    assert result["target_window_id"] == "@9"
    assert ("send-keys", "-t", "@9", "Down") in calls
    assert ("send-keys", "-t", "@9", "C-m") in calls


@pytest.mark.asyncio
async def test_worker_handles_codex_inline_hooks_review_with_trust_key(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(
        workdir=tmp_path,
        ready_timeout=1,
        codex_poll_interval=0,
        codex_ready_settle_time=0,
    )
    screens = iter(
        [
            "Resumed transcript\n"
            "1. Historical numbered row\n"
            "2. Another historical row\n"
            "⚠ 2 hooks need review before they can run.\n"
            "Press t to trust all; enter to review hooks; esc to close",
            "OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project",
        ]
    )
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        if args[0] == "capture-pane":
            return 0, next(screens), ""
        if args[0] == "display-message":
            return 0, "codex", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    result = await executor.create_session(
        path=str(tmp_path), backend="codex", name="inline-hooks"
    )

    assert result["target_window_id"] == "@9"
    assert ("send-keys", "-t", "@9", "t") in calls
    assert ("send-keys", "-t", "@9", "Down") not in calls


@pytest.mark.asyncio
async def test_worker_keeps_existing_model_at_codex_migration_prompt(
    tmp_path, monkeypatch
):
    executor = TmuxWorkerExecutor(
        workdir=tmp_path,
        ready_timeout=1,
        codex_poll_interval=0,
        codex_ready_settle_time=0,
    )
    screens = iter(
        [
            "OpenAI Codex (v0.155.1)\n"
            "Meet GPT-6 Sol\n"
            "Choose how you'd like Codex to proceed.\n"
            "1. Try new model\n"
            "2. Use existing model\n"
            "Use ↑/↓ to move, press enter to confirm",
            "OpenAI Codex\n\n› Ask anything\n\ngpt-5.6 medium · ~/project",
        ]
    )
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "list-windows":
            return 1, "", ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "new-window":
            return 0, "@9\n", ""
        if args[0] == "capture-pane":
            return 0, next(screens), ""
        if args[0] == "display-message":
            return 0, "codex", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)

    result = await executor.create_session(
        path=str(tmp_path), backend="codex", name="existing-model"
    )

    assert result["target_window_id"] == "@9"
    assert ("send-keys", "-t", "@9", "Down") in calls
    assert ("send-keys", "-t", "@9", "C-m") in calls


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


@pytest.mark.asyncio
async def test_control_command_is_not_blocked_by_full_regular_queue(
    tmp_path, monkeypatch
):
    import ccbot.node_agent as node_agent_module

    monkeypatch.setattr(node_agent_module, "_COMMAND_QUEUE_SIZE", 1)

    class BlockingExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.regular_started = asyncio.Event()
            self.release_regular = asyncio.Event()
            self.key_sent = asyncio.Event()

        async def list_directories(self, *, path: str):
            self.regular_started.set()
            await self.release_regular.wait()
            return {"ok": True, "path": path, "directories": []}

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
            request_id="regular-1",
            payload={"operation": "list_directories", "path": "/worker"},
        )
    )
    await asyncio.wait_for(executor.regular_started.wait(), timeout=1)
    for request_id in ("regular-2", "regular-3"):
        await transport.incoming.put(
            NodeEnvelope(
                kind="command",
                request_id=request_id,
                payload={"operation": "list_directories", "path": "/worker"},
            )
        )
    await transport.incoming.put(
        NodeEnvelope(
            kind="command",
            request_id="escape",
            payload={"operation": "send_key", "session_id": "s1", "key": "Escape"},
        )
    )

    try:
        await asyncio.wait_for(executor.key_sent.wait(), timeout=0.2)
    finally:
        executor.release_regular.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
