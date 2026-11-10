"""The sequential PTB update worker stays usable during real worker/file I/O."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ExtBot,
    MessageHandler,
    filters,
)
from telegram.request import BaseRequest

from ccbot import rich
from ccbot.bot import callbacks, inbound
from ccbot.bot.callbacks import file_buttons
from ccbot.file_actions import RemoteFileButtonContext, RemoteFileReference
from ccbot.file_delivery import FileDeliveryManager
from ccbot.handlers.card_types import CardState
from ccbot.inbound_queue import shutdown_inbound_queues
from ccbot.node_agent import NodeAgent
from ccbot.node_inbox import WorkerInboxMixin
from ccbot.node_runtime import RemoteNodeRuntime, connect_leader_rpc
from ccbot.node_transport import RelayServer, connect_relay
from ccbot.session import Session, session_manager


class TelegramEndpoint(BaseRequest):
    def __init__(self, blocked, release, *, hold_upload):
        self.blocked, self.release = blocked, release
        self.hold_upload = hold_upload
        self.acknowledged = []
        self.documents = []
        self.other_button = asyncio.Event()

    @property
    def read_timeout(self):
        return 10.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **_kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        parameters = request_data.parameters if request_data else {}
        if endpoint == "getMe":
            result = {
                "id": 1,
                "is_bot": True,
                "first_name": "Test",
                "username": "test_bot",
            }
        elif endpoint == "answerCallbackQuery":
            self.acknowledged.append(parameters["callback_query_id"])
            if parameters["callback_query_id"] == "other":
                self.other_button.set()
            result = True
        elif endpoint == "sendDocument":
            if self.hold_upload:
                self.blocked.set()
                await self.release.wait()
            parts = request_data.multipart_data or {}
            self.documents.extend(value[1] for value in parts.values())
            result = {
                "message_id": 500,
                "date": 1,
                "chat": {"id": 42, "type": "private"},
                "document": {"file_id": "delivered", "file_unique_id": "unique"},
            }
        else:
            raise AssertionError(f"Unexpected endpoint {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class FileWorker(WorkerInboxMixin):
    def __init__(self, workdir, blocked, release, *, hold_fetch):
        self.workdir, self.blocked, self.release = workdir, blocked, release
        self.hold_fetch = hold_fetch

    async def _find_session(self, session_id):
        return (
            SimpleNamespace(workdir=self.workdir)
            if session_id == "worker-session"
            else None
        )

    async def read_session_file(self, **kwargs):
        if self.hold_fetch:
            self.blocked.set()
            await self.release.wait()
        return await super().read_session_file(**kwargs)


def callback_update(bot, number, query_id, data):
    return Update.de_json(
        {
            "update_id": number,
            "callback_query": {
                "id": query_id,
                "chat_instance": "test",
                "data": data,
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "message": {
                    "message_id": 10,
                    "date": 1,
                    "chat": {"id": 42, "type": "private"},
                },
            },
        },
        bot,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["local_upload", "worker_fetch", "worker_upload"])
async def test_sequential_dispatcher_accepts_text_and_button_during_file_delivery(
    tmp_path, monkeypatch, phase
):
    payload = b"complete worker/local file"
    artifact = tmp_path / "report.zip"
    artifact.write_bytes(payload)
    blocked, release = asyncio.Event(), asyncio.Event()
    transport = TelegramEndpoint(blocked, release, hold_upload=phase != "worker_fetch")
    bot = ExtBot("1:testing", request=transport, get_updates_request=transport)
    application = (
        Application.builder().bot(bot).updater(None).concurrent_updates(False).build()
    )
    manager = FileDeliveryManager(staging_dir=tmp_path / "staging")
    monkeypatch.setattr(file_buttons, "file_delivery_manager", manager)
    source = Session(
        id="source", name="Source", window_id="@source", workdir=str(tmp_path)
    )
    monkeypatch.setattr(session_manager, "sessions", {"source": source})
    monkeypatch.setattr(session_manager, "active_sessions", {42: "source"})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *_: False)
    monkeypatch.setattr(inbound, "get_card_state", lambda *_: CardState())
    monkeypatch.setattr(inbound, "schedule_card_after_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(inbound, "refresh_session_keyboard", _nothing)
    monkeypatch.setattr(callbacks, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(callbacks, "_repaint_acknowledged_marker", _nothing)
    text_processed = asyncio.Event()

    async def process_text(update, _context, wid):
        assert wid == "@source" and update.message.text == "next request"
        text_processed.set()
        return True

    monkeypatch.setattr(inbound, "_run_text", process_text)
    application.add_handler(CallbackQueryHandler(callbacks.callback_handler))
    application.add_handler(MessageHandler(filters.TEXT, inbound.text_intake_handler))
    errors = []

    async def error(_update, context):
        errors.append(context.error)

    application.add_error_handler(error)
    server = leader = worker_transport = worker_task = None
    try:
        if phase.startswith("worker"):
            server = RelayServer(
                credentials={"leader": "leader-secret", "worker-a": "worker-secret"},
                leader_id="leader",
            )
            await server.start("127.0.0.1", 0)
            leader = await connect_leader_rpc(
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
            executor = FileWorker(
                tmp_path, blocked, release, hold_fetch=phase == "worker_fetch"
            )
            agent = NodeAgent(
                worker_transport,
                executor,
                context_dir=tmp_path / "contexts",
                node_id="worker-a",
                display_name="Worker A",
                backends=("codex",),
            )
            worker_task = asyncio.create_task(agent.run())
            runtime = RemoteNodeRuntime(leader, chunk_size=8)
            monkeypatch.setattr(
                "ccbot.file_delivery.get_node_runtime", lambda _: runtime
            )
            reference = RemoteFileReference(
                node_id="worker-a",
                session_id="worker-session",
                path=str(artifact),
                name=artifact.name,
                size=len(payload),
            )
            markup = rich.to_rich_markdown(
                str(artifact),
                file_base_dir=RemoteFileButtonContext({str(artifact): reference}),
            )
        else:
            markup = rich.to_rich_markdown(str(artifact))
        token = re.search(r'data="file:([0-9a-f]+)"', markup).group(1)
        async with application:
            await application.start()
            try:
                await application.update_queue.put(
                    callback_update(bot, 1, "file", "file:" + token)
                )
                await asyncio.wait_for(blocked.wait(), timeout=3)
                assert transport.acknowledged == ["file"]
                await application.update_queue.put(
                    callback_update(bot, 2, "duplicate", "file:" + token)
                )
                await application.update_queue.put(
                    Update.de_json(
                        {
                            "update_id": 3,
                            "message": {
                                "message_id": 30,
                                "date": 1,
                                "chat": {"id": 42, "type": "private"},
                                "from": {
                                    "id": 42,
                                    "is_bot": False,
                                    "first_name": "Test",
                                },
                                "text": "next request",
                            },
                        },
                        bot,
                    )
                )
                await application.update_queue.put(
                    callback_update(bot, 4, "other", "noop")
                )
                await asyncio.wait_for(
                    asyncio.gather(
                        text_processed.wait(), transport.other_button.wait()
                    ),
                    timeout=2,
                )
                assert not release.is_set() and transport.documents == []
                assert "duplicate" in transport.acknowledged
                release.set()
                await asyncio.wait_for(manager.wait_for_idle(), timeout=3)
                assert transport.documents == [payload]
                assert errors == []
                assert not list((tmp_path / "staging").glob("delivery-*"))
            finally:
                release.set()
                await manager.shutdown()
                await application.stop()
    finally:
        await shutdown_inbound_queues()
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        if worker_transport:
            await worker_transport.close()
        if leader:
            await leader.close()
        if server:
            await server.close()


async def _nothing(*_args, **_kwargs):
    pass
