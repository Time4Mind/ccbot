from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccbot.node_agent import NodeAgent
from ccbot.node_runtime import RemoteNodeRuntime, connect_leader_rpc
from ccbot.node_transport import RelayServer, connect_relay
from ccbot.session_models import Session
from ccbot.transfer_models import SessionTransfer


class RecordingExecutor:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    async def start_context_session(self, **kwargs):
        self.started.append(kwargs)
        return {
            "ok": True,
            "target_window_id": "@worker:1",
            "target_workdir": "/worker",
            "target_agent_session_id": "worker-session",
        }

    async def send_text(self, *, session_id: str, text: str):
        self.sent.append((session_id, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_context_transfer_crosses_real_relay_and_worker_agent(tmp_path: Path):
    server = RelayServer(
        credentials={"leader": "leader-secret", "worker-a": "worker-secret"},
        leader_id="leader",
    )
    await server.start("127.0.0.1", 0)
    leader_rpc = None
    worker_transport = None
    worker_task: asyncio.Task[None] | None = None
    try:
        leader_rpc = await connect_leader_rpc(
            host="127.0.0.1",
            port=server.port,
            leader_id="leader",
            secret="leader-secret",
        )
        worker_transport = await connect_relay(
            "127.0.0.1",
            server.port,
            node_id="worker-a",
            role="worker",
            secret="worker-secret",
            leader_id="leader",
        )
        executor = RecordingExecutor()
        agent = NodeAgent(
            worker_transport,
            executor,
            context_dir=tmp_path / "worker-contexts",
            node_id="worker-a",
            display_name="Worker A",
            backends=("codex",),
        )
        worker_task = asyncio.create_task(agent.run())

        context_path = tmp_path / "context.md"
        context_path.write_text("full transferred context", encoding="utf-8")
        source = Session(
            id="source",
            name="Transferred task",
            backend="claude",
            node_id="local",
        )
        transfer = SessionTransfer(
            id="transfer",
            source_session_id=source.id,
            source_node_id="local",
            target_node_id="worker-a",
            target_backend="codex",
            context_path=str(context_path),
        )

        result = await RemoteNodeRuntime(
            leader_rpc, chunk_size=4
        ).start_context_transfer(
            transfer=transfer,
            source=source,
            user_id=42,
            bot=SimpleNamespace(),
        )

        assert result.target_agent_session_id == "worker-session"
        assert executor.started[0]["backend"] == "codex"
        assert Path(executor.started[0]["context_path"]).read_text(
            encoding="utf-8"
        ) == ("full transferred context")

        update = SimpleNamespace(message=SimpleNamespace(text="continue"))
        assert await result.delivery(update, SimpleNamespace())
        assert executor.sent == [("worker-session", "continue")]
    finally:
        if leader_rpc is not None:
            await leader_rpc.close()
        if worker_transport is not None:
            await worker_transport.close()
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await server.close()
