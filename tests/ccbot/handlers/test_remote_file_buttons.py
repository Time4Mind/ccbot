from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import file_actions, rich
from ccbot.bot.callbacks import file_buttons
from ccbot.file_actions import RemoteFileButtonContext, RemoteFileReference
from ccbot.handlers import card_transport, message_sender
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.handlers.card_model import CardState
from ccbot.session_models import Session


@pytest.mark.asyncio
async def test_remote_card_renders_validated_worker_file_button(monkeypatch) -> None:
    worker_path = "/home/home/projects/report.zip"
    session = Session(
        id="session-1",
        name="Remote",
        node_id="worker-a",
        window_id="worker-a::@4",
        worker_session_id="routing-1",
        workdir="/home/home/projects",
    )
    runtime = SimpleNamespace(
        stat_session_file=AsyncMock(
            return_value={
                "ok": True,
                "path": worker_path,
                "name": "report.zip",
                "size": 7,
            }
        )
    )
    rendered: list[str] = []

    async def send_text(_bot, _user_id, text, *, file_base_dir=None, **_kwargs):
        rendered.append(rich.to_rich_markdown(text, file_base_dir=file_base_dir))
        return SimpleNamespace(message_id=100)

    monkeypatch.setattr(
        "ccbot.file_actions.get_node_runtime", lambda _node: runtime, raising=False
    )
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: False)
    monkeypatch.setattr(card_transport, "_strip_stale_switchers", AsyncMock())
    monkeypatch.setattr(card_transport, "_register_msg", lambda *_args: None)
    monkeypatch.setattr(card_transport, "bind_carrier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(card_transport, "remember_rich_photo", lambda *_args: None)
    monkeypatch.setattr(
        card_transport, "persist_session_screenshot", lambda *_args: None
    )
    monkeypatch.setattr(card_transport.session_manager, "set_card_msg", lambda *_: None)
    monkeypatch.setattr(
        card_transport.session_manager, "set_last_switcher_msg", lambda *_: None
    )
    monkeypatch.setattr(message_sender, "send_with_fallback", send_text)

    assert await card_transport._send_card_locked(
        object(),
        42,
        session,
        CardState(),
        text=f"Ready: `{worker_path}`",
        reply_markup=SimpleNamespace(),
    )

    runtime.stat_session_file.assert_awaited_once_with(
        "worker-a", "routing-1", worker_path
    )
    assert worker_path not in rendered[0]
    assert ">zip</tg-button>" in rendered[0]


@pytest.mark.asyncio
async def test_remote_card_edit_keeps_worker_file_button(monkeypatch) -> None:
    worker_path = "/worker/project/final.svg"
    session = Session(
        id="session-edit",
        name="Remote",
        node_id="worker-b",
        window_id="worker-b::@8",
        worker_session_id="routing-edit",
        workdir="/worker/project",
    )
    runtime = SimpleNamespace(
        stat_session_file=AsyncMock(
            return_value={
                "ok": True,
                "path": worker_path,
                "name": "final.svg",
                "size": 6,
            }
        )
    )
    rendered: list[str] = []

    async def edit(_bot, _uid, _mid, text, **kwargs):
        rendered.append(
            rich.to_rich_markdown(text, file_base_dir=kwargs["file_base_dir"])
        )
        return True

    monkeypatch.setattr("ccbot.file_actions.get_node_runtime", lambda _node: runtime)
    monkeypatch.setattr(
        card_transport, "lookup_session_for_message", lambda _uid, _mid: session.id
    )
    monkeypatch.setattr(
        card_transport.session_manager, "get_session", lambda _sid: session
    )
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: False)
    monkeypatch.setattr(message_sender, "try_rich_edit", edit)

    assert await card_transport._edit_card_unlocked(
        object(),
        42,
        CardState(msg_id=101),
        text=f"Ready: `{worker_path}`",
        reply_markup=SimpleNamespace(),
    )
    runtime.stat_session_file.assert_awaited_once()
    assert worker_path not in rendered[0]
    assert ">svg</tg-button>" in rendered[0]


@pytest.mark.asyncio
async def test_remote_file_button_downloads_exact_bytes(monkeypatch) -> None:
    reference = RemoteFileReference(
        node_id="worker-a",
        session_id="routing-1",
        path="/worker/project/report.zip",
        name="report.zip",
        size=7,
    )
    rendered = rich.to_rich_markdown(
        f"`{reference.path}`",
        file_base_dir=RemoteFileButtonContext({reference.path: reference}),
    )
    match = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert match is not None
    runtime = SimpleNamespace(
        download_session_file=AsyncMock(
            return_value={"ok": True, "name": "report.zip", "content": b"payload"}
        )
    )
    monkeypatch.setattr(file_buttons, "get_node_runtime", lambda _node: runtime)
    query = SimpleNamespace(data=f"file:{match.group(1)}", answer=AsyncMock())
    delivered: list[bytes] = []

    async def send_document(**kwargs):
        delivered.append(kwargs["document"].read())

    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=send_document), send_message=AsyncMock()
    )

    assert await file_buttons.handle(
        query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
    )
    runtime.download_session_file.assert_awaited_once_with(
        "worker-a", "routing-1", reference.path
    )
    assert bot.send_document.await_args.kwargs["filename"] == "report.zip"
    assert delivered == [b"payload"]


@pytest.mark.asyncio
async def test_remote_file_button_reports_offline_worker(monkeypatch) -> None:
    reference = RemoteFileReference(
        node_id="worker-a",
        session_id="routing-1",
        path="/worker/project/report.zip",
        name="report.zip",
        size=7,
    )
    rendered = rich.to_rich_markdown(
        reference.path,
        file_base_dir=RemoteFileButtonContext({reference.path: reference}),
    )
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None
    monkeypatch.setattr(file_buttons, "get_node_runtime", lambda _node: None)
    query = SimpleNamespace(data=f"file:{token.group(1)}", answer=AsyncMock())
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())

    assert await file_buttons.handle(
        query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
    )
    query.answer.assert_awaited_once_with("Файл больше недоступен", show_alert=True)
    bot.send_document.assert_not_awaited()


def test_remote_file_token_survives_registry_reload(monkeypatch, tmp_path) -> None:
    registry = tmp_path / "file-buttons.json"
    monkeypatch.setattr(file_actions, "_REGISTRY_FILE", registry)
    monkeypatch.setattr(file_actions, "_file_paths", {})
    monkeypatch.setattr(file_actions, "_registry_loaded", False)
    reference = RemoteFileReference(
        node_id="worker-a",
        session_id="routing-1",
        path="/worker/project/report.zip",
        name="report.zip",
        size=7,
    )
    rendered = rich.to_rich_markdown(
        reference.path,
        file_base_dir=RemoteFileButtonContext({reference.path: reference}),
    )
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None

    monkeypatch.setattr(file_actions, "_file_paths", {})
    monkeypatch.setattr(file_actions, "_registry_loaded", False)
    restored = file_actions.resolve_file_reference(token.group(1))

    assert isinstance(restored, RemoteFileReference)
    assert restored == reference


@pytest.mark.asyncio
async def test_worker_file_access_is_scoped_to_session_workdir(
    monkeypatch, tmp_path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    artifact = workdir / "report.zip"
    artifact.write_bytes(b"payload")
    outside = tmp_path / "secret.zip"
    outside.write_bytes(b"secret")
    executor = TmuxWorkerExecutor(workdir=workdir)
    monkeypatch.setattr(
        executor,
        "_find_session",
        AsyncMock(return_value=SimpleNamespace(workdir=workdir)),
    )

    result = await executor.stat_session_file(
        session_id="routing-1", path=str(artifact)
    )
    assert result["name"] == "report.zip"
    assert result["size"] == 7
    with pytest.raises(ValueError, match="unavailable"):
        await executor.stat_session_file(session_id="routing-1", path=str(outside))
    with pytest.raises(ValueError, match="unavailable"):
        await executor.stat_session_file(session_id="routing-1", path="../secret.zip")

    inbox = workdir / ".ccbot-inbox"
    inbox.mkdir()
    relative = inbox / "worker.svg"
    relative.write_bytes(b"<svg/>")
    relative_result = await executor.stat_session_file(
        session_id="routing-1", path=".ccbot-inbox/worker.svg"
    )
    assert relative_result["path"] == str(relative.resolve())
