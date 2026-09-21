from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from ccbot.bot import messages
from ccbot.node_models import Node
from ccbot.node_agent import NodeAgent
from ccbot.node_reconcile import schedule_remote_reconcile, shutdown_remote_reconcile
from ccbot.node_runtime import RemoteNodeRuntime
from ccbot.node_transport import NodeEnvelope
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.session import SessionManager
from ccbot.session_models import WindowState
from ccbot import session_recovery


@pytest.mark.asyncio
async def test_remote_runtime_uploads_in_chunks_and_finishes_after_ack() -> None:
    content = b"worker-image" * 100_000
    rpc = SimpleNamespace(request=AsyncMock())
    rpc.request.side_effect = [
        {"ok": True, "upload_id": "upload-1"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "relative_path": ".ccbot-inbox/photo.jpg"},
    ]
    runtime = RemoteNodeRuntime(rpc, chunk_size=1024 * 1024)

    result = await runtime.upload_inbox_file(
        "worker-a", "routing-1", "../../photo.jpg", content
    )

    assert result["relative_path"] == ".ccbot-inbox/photo.jpg"
    calls = rpc.request.await_args_list
    assert [call.args[1] for call in calls] == [
        "upload_inbox_begin",
        "upload_inbox_chunk",
        "upload_inbox_chunk",
        "upload_inbox_finish",
    ]
    assert calls[0].args[2]["sha256"] == hashlib.sha256(content).hexdigest()
    assert calls[-1].args[2]["upload_id"] == "upload-1"


@pytest.mark.asyncio
async def test_remote_photo_upload_precedes_prompt_without_local_tmux(
    monkeypatch, tmp_path: Path
) -> None:
    downloaded = SimpleNamespace(download_to_drive=AsyncMock())
    photo = SimpleNamespace(
        file_unique_id="photo-id", get_file=AsyncMock(return_value=downloaded)
    )
    msg = SimpleNamespace(
        caption="look",
        photo=[photo],
        document=None,
        forward_origin=None,
        via_bot=None,
    )
    update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=42))
    context = SimpleNamespace(bot=object())
    sess = SimpleNamespace(
        id="s1",
        node_id="worker-a",
        worker_session_id="routing-1",
        claude_session_id="",
        workdir="/worker/project",
    )
    runtime = SimpleNamespace(
        upload_inbox_file=AsyncMock(
            return_value={"ok": True, "relative_path": ".ccbot-inbox/photo.jpg"}
        )
    )
    delivered: list[str] = []

    async def download(target: Path) -> None:
        target.write_bytes(b"image")

    downloaded.download_to_drive.side_effect = download

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    async def send(_wid, text, _sess):
        delivered.append(text)
        assert runtime.upload_inbox_file.await_count == 1
        return True, "ok"

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "worker-a::@18")
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket)
    monkeypatch.setattr(
        messages,
        "prepare_request_for_dispatch",
        AsyncMock(
            return_value=SimpleNamespace(text="look", confirm_delivery=MagicMock())
        ),
    )
    monkeypatch.setattr(messages, "_send_with_delivery_proof", send)
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    local_lookup = AsyncMock(side_effect=AssertionError("remote id reached local tmux"))
    monkeypatch.setattr(
        messages, "tmux_manager", SimpleNamespace(find_window_by_id=local_lookup)
    )
    monkeypatch.setattr(
        messages, "get_node_runtime", lambda _node_id: runtime, raising=False
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_display_name=lambda _wid: "Remote",
        ),
    )

    assert await messages.photo_handler(update, context)
    runtime.upload_inbox_file.assert_awaited_once()
    assert runtime.upload_inbox_file.await_args.args[:3] == (
        "worker-a",
        "routing-1",
        "photo-id.jpg",
    )
    assert delivered == ["look\n\n.ccbot-inbox/photo.jpg"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_remote_document_upload_failure_sends_no_prompt(
    monkeypatch,
) -> None:
    downloaded = SimpleNamespace(download_to_drive=AsyncMock())
    document = SimpleNamespace(
        file_unique_id="doc-id",
        file_name="notes.txt",
        get_file=AsyncMock(return_value=downloaded),
    )
    msg = SimpleNamespace(caption="read", photo=None, document=document)
    update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=42))
    context = SimpleNamespace(bot=object())
    sess = SimpleNamespace(
        id="s1",
        node_id="worker-a",
        worker_session_id="routing-1",
        claude_session_id="",
        workdir="/worker/project",
    )
    runtime = SimpleNamespace(
        upload_inbox_file=AsyncMock(side_effect=ConnectionError("worker offline"))
    )
    sent = AsyncMock()

    async def download(target: Path) -> None:
        target.write_bytes(b"document")

    downloaded.download_to_drive.side_effect = download
    reply = AsyncMock()
    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "worker-a::@18")
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(messages, "safe_reply", reply)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent)
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_display_name=lambda _wid: "Remote",
        ),
    )

    assert await messages.document_handler(update, context) is False
    sent.assert_not_awaited()
    assert "not delivered" in reply.await_args.args[1]


@pytest.mark.asyncio
async def test_forwarded_rich_photo_uploads_to_worker(monkeypatch) -> None:
    msg = Message.de_json(
        {
            "message_id": 5,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "forward_origin": {
                "type": "user",
                "date": 0,
                "sender_user": {
                    "id": 99,
                    "is_bot": True,
                    "first_name": "Source",
                    "username": "source_bot",
                },
            },
            "rich_message": {
                "blocks": [
                    {"type": "paragraph", "text": "Review"},
                    {
                        "type": "photo",
                        "photo": [
                            {
                                "file_id": "photo-id",
                                "file_unique_id": "unique-id",
                                "width": 100,
                                "height": 100,
                            }
                        ],
                    },
                ]
            },
        },
        None,
    )
    assert msg is not None
    update = SimpleNamespace(message=msg, effective_user=msg.from_user)
    sess = SimpleNamespace(
        id="s1",
        node_id="worker-a",
        worker_session_id="routing-1",
        claude_session_id="",
        workdir="/worker/project",
    )
    tg_file = SimpleNamespace(download_to_drive=AsyncMock())

    async def download(target: Path) -> None:
        target.write_bytes(b"rich-image")

    tg_file.download_to_drive.side_effect = download
    bot = SimpleNamespace(get_file=AsyncMock(return_value=tg_file))
    runtime = SimpleNamespace(
        upload_inbox_file=AsyncMock(
            return_value={"ok": True, "relative_path": ".ccbot-inbox/rich.jpg"}
        )
    )
    sent = AsyncMock(return_value=(True, "ok"))

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "worker-a::@18")
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket)
    monkeypatch.setattr(
        messages,
        "prepare_request_for_dispatch",
        AsyncMock(
            return_value=SimpleNamespace(text="Review", confirm_delivery=MagicMock())
        ),
    )
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent)
    monkeypatch.setattr(messages, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            find_session_by_window=lambda _wid: sess,
            get_display_name=lambda _wid: "Remote",
            touch_session=lambda _sid: None,
        ),
    )

    assert await messages.unsupported_content_handler(update, SimpleNamespace(bot=bot))
    runtime.upload_inbox_file.assert_awaited_once()
    assert runtime.upload_inbox_file.await_args.args[3] == b"rich-image"
    assert sent.await_args.args[1] == (
        "[forwarded from @source_bot]\nReview\n.ccbot-inbox/rich.jpg"
    )


@pytest.mark.asyncio
async def test_worker_upload_is_atomic_sanitized_and_idempotent(
    monkeypatch, tmp_path: Path
) -> None:
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    session = SimpleNamespace(window_id="@1", backend="codex", workdir=tmp_path)
    monkeypatch.setattr(executor, "_find_session", AsyncMock(return_value=session))
    content = b"complete-image"
    digest = hashlib.sha256(content).hexdigest()
    begin = await executor.upload_inbox_begin(
        upload_id="stable-upload",
        session_id="routing-1",
        filename="../../unsafe name.jpg",
        total_bytes=len(content),
        sha256=digest,
    )
    assert begin["ok"] is True
    await executor.upload_inbox_chunk(
        upload_id="stable-upload",
        chunk_index=0,
        data=__import__("base64").b64encode(content).decode("ascii"),
    )
    first = await executor.upload_inbox_finish(upload_id="stable-upload")
    second = await executor.upload_inbox_finish(upload_id="stable-upload")

    assert first == second
    target = tmp_path / first["relative_path"]
    assert target.read_bytes() == content
    assert target.name.endswith("-unsafe_name.jpg")
    assert not list((tmp_path / ".ccbot-inbox").glob("*.part"))


@pytest.mark.asyncio
async def test_failed_worker_upload_leaves_no_partial_file(
    monkeypatch, tmp_path: Path
) -> None:
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    session = SimpleNamespace(window_id="@1", backend="codex", workdir=tmp_path)
    monkeypatch.setattr(executor, "_find_session", AsyncMock(return_value=session))
    await executor.upload_inbox_begin(
        upload_id="broken-upload",
        session_id="routing-1",
        filename="photo.jpg",
        total_bytes=5,
        sha256=hashlib.sha256(b"right").hexdigest(),
    )
    await executor.upload_inbox_chunk(
        upload_id="broken-upload",
        chunk_index=0,
        data=__import__("base64").b64encode(b"wrong").decode("ascii"),
    )

    with pytest.raises(ValueError, match="sha256 mismatch"):
        await executor.upload_inbox_finish(upload_id="broken-upload")

    assert not list((tmp_path / ".ccbot-inbox").iterdir())


@pytest.mark.asyncio
async def test_authenticated_agent_dispatches_worker_upload(
    monkeypatch, tmp_path: Path
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.sent: list[NodeEnvelope] = []

        async def send(self, message: NodeEnvelope) -> None:
            self.sent.append(message)

    executor = TmuxWorkerExecutor(workdir=tmp_path)
    session = SimpleNamespace(window_id="@1", backend="codex", workdir=tmp_path)
    monkeypatch.setattr(executor, "_find_session", AsyncMock(return_value=session))
    transport = Transport()
    agent = NodeAgent(transport, executor, context_dir=tmp_path / "context")
    content = b"photo"
    await agent._handle_command(
        NodeEnvelope(
            kind="command",
            request_id="upload-begin",
            payload={
                "operation": "upload_inbox_begin",
                "upload_id": "upload-1",
                "session_id": "routing-1",
                "filename": "photo.jpg",
                "total_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
    )

    assert transport.sent[-1].kind == "result"
    assert transport.sent[-1].payload == {"ok": True, "upload_id": "upload-1"}


@pytest.fixture
def manager(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    return SessionManager()


@pytest.mark.asyncio
async def test_startup_local_recovery_preserves_remote_binding(
    manager: SessionManager, monkeypatch
) -> None:
    manager.register_node(Node(id="worker-a", display_name="Worker A", state="offline"))
    session = manager.create_session(
        name="Remote",
        window_id="worker-a::@18",
        workdir="/worker/project",
        node_id="worker-a",
        worker_session_id="routing-1",
    )
    manager.active_sessions[42] = session.id
    manager.active_sessions_by_node[42] = {"worker-a": session.id}
    manager.window_states[session.window_id] = WindowState(window_name="Remote")
    monkeypatch.setattr(
        session_recovery.tmux_manager, "list_windows", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        session_recovery, "cleanup_stale_session_map_entries", AsyncMock()
    )
    monkeypatch.setattr(
        session_recovery, "cleanup_old_format_session_map_keys", AsyncMock()
    )
    monkeypatch.setattr(
        session_recovery, "cleanup_orphan_grouped_sessions", MagicMock()
    )
    monkeypatch.setattr(session_recovery, "detect_orphan_windows", AsyncMock())

    await session_recovery.resolve_stale_window_ids(manager)
    lost = await session_recovery.reconcile_with_tmux(manager)

    assert lost == 0
    assert session.state == "active"
    assert manager.active_sessions[42] == session.id
    assert manager.active_sessions_by_node[42]["worker-a"] == session.id
    assert "worker-a::@18" in manager.window_states


def test_composite_worker_window_id_is_not_legacy(manager: SessionManager) -> None:
    assert manager.is_window_id("worker-a::@18") is True


@pytest.mark.asyncio
async def test_authoritative_worker_rebind_and_not_found_transition(
    manager: SessionManager,
) -> None:
    session = manager.create_session(
        name="Remote",
        window_id="worker-a::@18",
        workdir="/worker/project",
        node_id="worker-a",
        worker_session_id="routing-1",
    )
    manager.window_states[session.window_id] = WindowState(window_name="Remote")
    runtime = SimpleNamespace(
        inspect_session=AsyncMock(
            return_value={
                "ok": True,
                "found": True,
                "window_id": "@22",
                "workdir": "/worker/project",
            }
        )
    )

    await session_recovery.reconcile_remote_sessions(manager, "worker-a", runtime)

    assert session.window_id == "worker-a::@22"
    assert session.state == "active"
    assert "worker-a::@22" in manager.window_states
    runtime.inspect_session.return_value = {"ok": True, "found": False}

    await session_recovery.reconcile_remote_sessions(manager, "worker-a", runtime)

    assert session.state == "lost"


@pytest.mark.asyncio
async def test_worker_boot_change_reconciles_once_per_process(
    manager: SessionManager, monkeypatch
) -> None:
    from ccbot import node_reconcile

    session = manager.create_session(
        name="Remote",
        window_id="worker-a::@18",
        workdir="/worker/project",
        node_id="worker-a",
        worker_session_id="routing-1",
    )
    runtime = SimpleNamespace(
        inspect_session=AsyncMock(
            return_value={
                "ok": True,
                "found": True,
                "window_id": "@19",
                "workdir": "/worker/project",
            }
        )
    )
    monkeypatch.setattr(
        "ccbot.transfer_runtime.get_node_runtime", lambda _node_id: runtime
    )

    schedule_remote_reconcile(manager, "worker-a", "boot-1")
    await node_reconcile._tasks["worker-a"]
    schedule_remote_reconcile(manager, "worker-a", "boot-1")
    await asyncio.sleep(0)

    assert runtime.inspect_session.await_count == 1
    assert session.window_id == "worker-a::@19"
    runtime.inspect_session.return_value["window_id"] = "@20"
    schedule_remote_reconcile(manager, "worker-a", "boot-2")
    await node_reconcile._tasks["worker-a"]

    assert runtime.inspect_session.await_count == 2
    assert session.window_id == "worker-a::@20"
    await shutdown_remote_reconcile()
