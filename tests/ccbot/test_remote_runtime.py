from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccbot.node_runtime import RemoteNodeRuntime
from ccbot.session_models import Session
from ccbot.transfer_models import SessionTransfer


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request(
        self, target_node_id: str, operation: str, payload: dict, **_kwargs
    ):
        payload = {"target_node_id": target_node_id, **payload}
        self.calls.append((operation, dict(payload)))
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
