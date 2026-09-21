from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ccbot.handlers import archive
from ccbot.node_models import Node
from ccbot.session import SessionManager
from ccbot.session_models import Session


class RestoreRuntime:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {
            "target_window_id": "@12",
            "target_workdir": "/srv/project",
            "target_agent_session_id": "provider-12",
        }
        self.error = error
        self.calls: list[tuple[tuple, dict]] = []

    async def create_session(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _manager(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    manager = SessionManager()
    monkeypatch.setattr(manager, "save_state", lambda: None)
    manager.agent_backend = "codex"
    manager.register_node(
        Node(
            id="worker-a",
            display_name="Worker A",
            state="ready",
            backends=["claude", "codex"],
        )
    )
    monkeypatch.setattr(archive, "session_manager", manager)
    return manager


@pytest.mark.asyncio
async def test_remote_archive_restore_runs_on_owner_and_selects_it(
    monkeypatch,
) -> None:
    manager = _manager(monkeypatch)
    session = Session(
        id="archived",
        name="Remote task",
        state="archived",
        node_id="worker-a",
        workdir="/srv/project",
        backend="codex",
        claude_session_id="rollout-42",
    )
    manager.sessions[session.id] = session
    runtime = RestoreRuntime()
    monkeypatch.setattr(archive, "get_node_runtime", lambda _node_id: runtime)

    ok, _message = await archive.restore_session(MagicMock(), 42, session)

    assert ok is True
    assert runtime.calls == [
        (
            ("worker-a", "/srv/project", "codex", "Remote task"),
            {"resume_session_id": "rollout-42", "source_backend": "codex"},
        )
    ]
    assert session.window_id == "worker-a::@12"
    assert session.node_id == "worker-a"
    assert session.claude_session_id == "provider-12"
    assert manager.get_selected_node_id(42) == "worker-a"
    assert manager.get_active_session(42) is session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,error_text",
    [
        (None, "not connected"),
        (
            RestoreRuntime(error=ConnectionError("worker workdir does not exist")),
            "worker workdir",
        ),
        (RestoreRuntime(error=ConnectionError("Codex rollout not found")), "rollout"),
    ],
)
async def test_remote_archive_restore_surfaces_worker_failure(
    monkeypatch, runtime, error_text
) -> None:
    manager = _manager(monkeypatch)
    session = Session(
        id="archived",
        name="Remote task",
        state="archived",
        node_id="worker-a",
        workdir="/srv/project",
        backend="codex",
        claude_session_id="rollout-42",
    )
    manager.sessions[session.id] = session
    monkeypatch.setattr(archive, "get_node_runtime", lambda _node_id: runtime)

    ok, message = await archive.restore_session(MagicMock(), 42, session)

    assert ok is False
    assert error_text.casefold() in message.casefold()
    assert session.state == "archived"
