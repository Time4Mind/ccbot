"""OpenCode must accept each queued prompt once while Stop stays available."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import tmux_input_transport
from ccbot.tmux_manager import TmuxManager

IDLE = "  ┃\n  ┃  Build · GPT-6 Luna OpenAI\n  /tmp/lab  24K (2%)  ctrl+p commands\n"
BUSY = IDLE.rsplit("  /tmp", 1)[0] + "  ■⬝⬝  esc interrupt  24K (2%) ctrl+p commands\n"


@pytest.mark.asyncio
async def test_opencode_long_unicode_is_one_literal_paste(monkeypatch):
    from ccbot import opencode_input

    calls = []

    async def run(*args, input_bytes=None):
        calls.append((args, input_bytes))
        return 0, b""

    async def submit(_window, _text, send):
        return await send()

    monkeypatch.setattr(opencode_input, "submit", submit)
    monkeypatch.setattr(tmux_input_transport, "_run_tmux", run)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    text = "first\n" + "абв🙂" * 350 + "\nlast"
    assert await tmux_input_transport.send_literal_chunked(
        "@7", text, backend="opencode"
    )
    loads = [data for args, data in calls if args[0] == "load-buffer"]
    assert loads == [text.encode("utf-8")]
    assert len([args for args, _ in calls if args[0] == "paste-buffer"]) == 1


@pytest.fixture
def native_input(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "opencode.db")
    db.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            parent_id TEXT, time_updated INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    db.execute("INSERT INTO session VALUES ('ses_input', '/tmp/lab','test',NULL,1)")
    db.commit()
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SESSION_DB_PATH", str(tmp_path / "opencode.db"))
    (tmp_path / "session_map.json").write_text(
        json.dumps(
            {
                "test:@7": {
                    "backend": "opencode",
                    "session_id": "ses_input",
                    "cwd": "/tmp/lab",
                }
            }
        )
    )
    yield db
    db.close()


@pytest.mark.asyncio
async def test_busy_opencode_preserves_prompt_and_allows_stop(
    native_input, monkeypatch
):
    idle = asyncio.Event()
    capture_seen = asyncio.Event()

    async def process(*args, **kwargs):
        if "capture-pane" in args:
            capture_seen.set()
            output = IDLE if idle.is_set() else BUSY
        else:
            output = "opencode"
        return SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(output.encode(), b""))
        )

    async def paste(window, text, *, backend):
        assert idle.is_set(), "second request pasted while OpenCode is busy"
        now = int(time.time() * 1000)
        native_input.execute(
            "INSERT INTO message VALUES ('m1','ses_input',?,?,?)",
            (now, now, json.dumps({"role": "user"})),
        )
        native_input.execute(
            "INSERT INTO part VALUES ('p1','m1','ses_input',?,?,?)",
            (now, now, json.dumps({"type": "text", "text": text})),
        )
        native_input.commit()
        return True

    monkeypatch.setattr(tmux_input_transport.asyncio, "create_subprocess_exec", process)
    raw = AsyncMock(side_effect=paste)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", raw)
    manager = TmuxManager()

    async def locked(window, text, enter, literal, *, backend):
        if literal:
            return await tmux_input_transport.send_literal_chunked(
                window, text, backend=backend
            )
        return True

    monkeypatch.setattr(manager, "_send_keys_locked", locked)
    task = asyncio.create_task(
        manager.send_keys("@7", "follow-up\n🙂", backend="opencode")
    )
    try:
        await asyncio.sleep(0.05)
        raw.assert_not_awaited()
        # A queued prompt must not hold the keyboard/navigation lock.
        assert await asyncio.wait_for(
            manager.send_keys(
                "@7", "Escape", enter=False, literal=False, backend="opencode"
            ),
            0.1,
        )
        idle.set()
        assert await asyncio.wait_for(task, 2)
        raw.assert_awaited_once_with("@7", "follow-up\n🙂", backend="opencode")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_matching_prompt_never_acknowledges_a_new_send(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    native_input.execute(
        "INSERT INTO message VALUES ('old','ses_input',1,1,?)",
        (json.dumps({"role": "user"}),),
    )
    native_input.execute(
        "INSERT INTO part VALUES ('old-part','old','ses_input',1,1,?)",
        (json.dumps({"type": "text", "text": "same prompt"}),),
    )
    native_input.execute(
        "INSERT INTO message VALUES ('done','ses_input',2,2,?)",
        (json.dumps({"role": "assistant", "time": {"completed": 2}}),),
    )
    native_input.commit()
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(opencode_input, "ACK_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)
    assert not await tmux_input_transport.send_literal_chunked(
        "@7", "same prompt", backend="opencode"
    )
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unready_or_dead_opencode_never_receives_queued_text(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=BUSY))
    monkeypatch.setattr(opencode_input, "READY_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=False)
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)
    assert not await tmux_input_transport.send_literal_chunked(
        "@7", "queued text", backend="opencode"
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_paste_timeout_notifies_and_releases_the_opencode_fifo(
    native_input, monkeypatch, tmp_path
):
    from telegram import Update

    from ccbot import input_recovery, opencode_input
    from ccbot.bot import _messages_shared
    from ccbot.inbound_queue import enqueue_inbound, shutdown_inbound_queues
    from ccbot.session import Session, session_manager

    recovery = input_recovery.InputRecovery(tmp_path / "inputs.json")
    recovery.bot = AsyncMock()
    recovery.bot.send_message.return_value.message_id = 500
    session = Session(id="s1", name="test", window_id="@7", backend="opencode")
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    monkeypatch.setattr(session_manager, "get_session", lambda _: session)
    monkeypatch.setattr(session_manager, "find_session_by_window", lambda _: session)
    monkeypatch.setattr(session_manager, "is_window_starting", lambda _: False)
    monkeypatch.setattr(session_manager, "touch_session", lambda _: None)
    monkeypatch.setattr(
        _messages_shared, "_voice_transcript_checkpoint", lambda _: None
    )
    monkeypatch.setattr(opencode_input, "READY_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    ready = asyncio.Event()

    async def capture(_window):
        entry = recovery.current()
        assert entry is not None
        if entry.message_id == 1:
            return BUSY
        await ready.wait()
        return IDLE

    async def paste(_window, text, *, backend):
        assert ready.is_set() and backend == "opencode"
        native_input.execute(
            "INSERT INTO message VALUES ('m2','ses_input',2,2,?)",
            (json.dumps({"role": "user"}),),
        )
        native_input.execute(
            "INSERT INTO part VALUES ('p2','m2','ses_input',2,2,?)",
            (json.dumps({"type": "text", "text": text}),),
        )
        native_input.commit()
        return True

    send = AsyncMock(side_effect=paste)
    monkeypatch.setattr(opencode_input, "_capture", capture)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)

    async def sender(wid, text, _session):
        accepted = await tmux_input_transport.send_literal_chunked(
            wid, text, backend="opencode"
        )
        return accepted, "Sent" if accepted else "tmux input failed"

    async def processor(update, _, wid):
        accepted, _ = await recovery.submit(wid, update.message.text, session, sender)
        return accepted

    def admit(message_id, text):
        update = Update.de_json(
            {
                "update_id": message_id,
                "message": {
                    "message_id": message_id,
                    "date": 1,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "Probe"},
                    "text": text,
                },
            },
            None,
        )
        request = recovery.admit(update, "text", session)
        assert request is not None
        receipt = enqueue_inbound(
            42,
            "@7",
            update,
            None,
            kind="text",
            processor=processor,
            recovery_id=request.id,
        )
        return request, receipt.completion

    try:
        first, first_completion = admit(1, "first")
        second, second_completion = admit(2, "second")
        assert not await asyncio.wait_for(first_completion, 1)
        send.assert_not_awaited()
        assert native_input.execute("SELECT count(*) FROM part").fetchone()[0] == 0
        assert first.state == "failed"
        reloaded = input_recovery.InputRecovery(recovery.path)
        reloaded.load()
        assert reloaded.records[first.id].state == "failed"
        await recovery.deliver_notice(first)
        recovery.bot.send_message.assert_awaited_once()
        notice = recovery.bot.send_message.await_args.kwargs
        assert notice["reply_parameters"].message_id == 1
        assert "Запрос не отправлен" in notice["text"]
        ready.set()
        assert await asyncio.wait_for(second_completion, 2)
        send.assert_awaited_once_with("@7", "second", backend="opencode")
        assert second.confirmed
    finally:
        await shutdown_inbound_queues()


@pytest.mark.asyncio
async def test_first_accepted_prompt_finishes_before_next_even_if_composer_idle(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    native_input.execute(
        "INSERT INTO message VALUES ('first','ses_input',1,1,?)",
        (json.dumps({"role": "user"}),),
    )
    native_input.execute(
        "INSERT INTO part VALUES ('first-part','first','ses_input',1,1,?)",
        (json.dumps({"type": "text", "text": "first"}),),
    )
    native_input.commit()
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)
    task = asyncio.create_task(
        tmux_input_transport.send_literal_chunked("@7", "second", backend="opencode")
    )
    try:
        await asyncio.sleep(0.05)
        send.assert_not_awaited()
        native_input.execute(
            "INSERT INTO message VALUES ('done','ses_input',2,2,?)",
            (json.dumps({"role": "assistant", "time": {"completed": 2}}),),
        )
        native_input.commit()
        assert not await asyncio.wait_for(task, 2)
        send.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("pasting", [False, True])
async def test_restart_replays_only_prompt_that_has_not_started_pasting(
    tmp_path, monkeypatch, pasting
):
    from ccbot import opencode_input
    from ccbot import input_recovery as recovery_module
    from ccbot.input_recovery import InputRecovery, InputRequest

    recovery = InputRecovery(tmp_path / "input.json")
    request = InputRequest(
        "r1", 42, 1, "s1", "@7", "text", {}, state="dispatching", prepared="queued"
    )
    recovery.records[request.id] = request
    recovery.save()
    monkeypatch.setattr(recovery, "current", lambda: request)
    monkeypatch.setattr(recovery_module, "input_recovery", recovery)
    captured = asyncio.Event()
    sending = asyncio.Event()

    async def capture(window):
        captured.set()
        return IDLE if pasting else BUSY

    async def send():
        sending.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(opencode_input, "_capture", capture)
    monkeypatch.setattr(opencode_input, "_turn_finished", lambda window: True)
    monkeypatch.setattr(opencode_input, "_accepted", lambda window: {})
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    task = asyncio.create_task(opencode_input.submit("@7", "queued", send))
    try:
        await (sending.wait() if pasting else captured.wait())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        restored = InputRecovery(recovery.path)
        restored.load()
        assert restored.records["r1"].state == ("unknown" if pasting else "prepared")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
