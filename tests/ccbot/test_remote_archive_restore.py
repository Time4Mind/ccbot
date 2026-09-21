from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ccbot.handlers import archive
from ccbot.node_models import Node
from ccbot.session import SessionManager
from ccbot.session_models import Session
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.config import config
import json
from unittest.mock import AsyncMock


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
        provider_transcript_path="/srv/codex/rollout-42.jsonl",
    )
    manager.sessions[session.id] = session
    runtime = RestoreRuntime()
    monkeypatch.setattr(archive, "get_node_runtime", lambda _node_id: runtime)

    ok, _message = await archive.restore_session(MagicMock(), 42, session)

    assert ok is True
    assert runtime.calls == [
        (
            ("worker-a", "/srv/project", "codex", "Remote task"),
            {
                "resume_session_id": "rollout-42",
                "source_backend": "codex",
                "provider_transcript_path": "/srv/codex/rollout-42.jsonl",
            },
        )
    ]
    assert session.window_id == "worker-a::@12"
    assert session.node_id == "worker-a"
    assert session.worker_session_id == "provider-12"
    assert session.claude_session_id == "rollout-42"
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


@pytest.mark.asyncio
async def test_remote_codex_create_bind_archive_restore_uses_native_rollout(
    tmp_path, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CCBOT_DIR", str(state_dir))
    home = tmp_path / "home"
    home.mkdir()
    custom_codex_home = home / ".codex-current"
    sessions_root = custom_codex_home / "sessions"
    stale_sessions_root = home / ".codex" / "sessions"
    day = stale_sessions_root / "2026" / "09" / "21"
    day.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(custom_codex_home))
    monkeypatch.setenv("CCBOT_CODEX_SESSIONS_PATH", str(sessions_root))
    monkeypatch.setattr(config, "codex_sessions_path", sessions_root)
    workdir = tmp_path / "project"
    workdir.mkdir()
    rollout = day / "rollout-provider.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "provider-01", "cwd": str(workdir)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []
    windows = iter(("@9", "@10"))
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    async def fake_tmux(*args: str):
        calls.append(args)
        if args[0] == "new-window":
            return 0, next(windows) + "\n", ""
        return 0, "", ""

    monkeypatch.setattr(executor, "_run_tmux", fake_tmux)
    monkeypatch.setattr(executor, "_wait_ready", AsyncMock())
    created = await executor.create_session(
        path=str(workdir), backend="codex", name="Remote"
    )
    routing_id = created["target_agent_session_id"]
    (state_dir / "session_map.json").write_text(
        json.dumps(
            {
                "ccbot-worker:@9": {
                    "session_id": "provider-01",
                    "transcript_path": str(rollout),
                }
            }
        ),
        encoding="utf-8",
    )
    binding = (await executor.poll_events())[0]
    manager = _manager(monkeypatch)
    session = manager.create_session(
        name="Remote",
        node_id="worker-a",
        workdir=str(workdir),
        backend="codex",
        worker_session_id=routing_id,
    )
    manager.bind_remote_session(
        "worker-a",
        binding["session_id"],
        binding["provider_session_id"],
        binding["transcript_path"],
    )
    manager.mark_session_archived(session.id)
    executor._sessions.clear()

    class Runtime:
        async def create_session(self, _node_id, path, backend, name, **kwargs):
            return await executor.create_session(
                path=path, backend=backend, name=name, **kwargs
            )

    monkeypatch.setattr(archive, "get_node_runtime", lambda _node_id: Runtime())

    ok, _message = await archive.restore_session(MagicMock(), 42, session)

    assert ok is True
    assert session.claude_session_id == "provider-01"
    assert session.worker_session_id
    assert session.worker_session_id != "provider-01"
    assert any(
        call[:3] == ("send-keys", "-t", "@10")
        and f"HOME={home}" in call[3]
        and f"CODEX_HOME={custom_codex_home}" in call[3]
        and f"CCBOT_CODEX_SESSIONS_PATH={sessions_root}" in call[3]
        and call[3].endswith("codex resume provider-01")
        for call in calls
    )


@pytest.mark.asyncio
async def test_remote_restore_rejects_mismatched_exact_transcript_path(
    tmp_path, monkeypatch
) -> None:
    sessions_root = tmp_path / "configured-sessions"
    day = sessions_root / "2026" / "09" / "21"
    day.mkdir(parents=True)
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.setattr(config, "codex_sessions_path", sessions_root)
    configured = day / "rollout-provider-01.jsonl"
    configured.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "provider-01", "cwd": str(workdir)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    wrong = tmp_path / "rollout-wrong.jsonl"
    wrong.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "other-provider", "cwd": str(workdir)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    executor = TmuxWorkerExecutor(workdir=tmp_path)

    with pytest.raises(ValueError, match="does not match"):
        await executor.create_session(
            path=str(workdir),
            backend="codex",
            name="Remote",
            resume_session_id="provider-01",
            source_backend="codex",
            provider_transcript_path=str(wrong),
        )


@pytest.mark.asyncio
async def test_remote_session_without_binding_is_preserved_as_archive(
    monkeypatch,
) -> None:
    manager = _manager(monkeypatch)
    session = manager.create_session(
        name="Pending binding",
        node_id="worker-a",
        workdir="/srv/project",
        backend="codex",
        worker_session_id="routing-42",
    )

    kept = await archive.archive_or_delete_session(session, completed=False)

    assert kept is True
    assert session.state == "archived"
