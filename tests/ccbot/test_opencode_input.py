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
BLOCKED = (
    "  ┃  △ Permission required\n"
    "  ┃  Approve terminal command\n"
    "  ┃  Allow once   Allow always   Reject\n"
    "  ┃\n"
)


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
    from ccbot.config import config

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
                f"{config.tmux_session_name}:@7": {
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
async def test_opencode_permission_keeps_prompt_waiting_and_allows_stop(
    native_input, monkeypatch
):
    idle = asyncio.Event()
    capture_seen = asyncio.Event()

    async def process(*args, **kwargs):
        if "capture-pane" in args:
            capture_seen.set()
            output = IDLE if idle.is_set() else BLOCKED
        else:
            output = "opencode"
        return SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(output.encode(), b""))
        )

    async def paste(window, text, *, backend):
        assert idle.is_set(), (
            "second request pasted while OpenCode shows an approval dialog"
        )
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
        "INSERT INTO message VALUES ('done','ses_input',3,3,?)",
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

    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=BLOCKED))
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
@pytest.mark.parametrize("scenario", ["wait", "exit", "restart", "photo"])
async def test_opencode_fifo_waits_past_readiness_deadline_or_reports_exit(
    native_input, monkeypatch, tmp_path, scenario
):
    from telegram import Update

    from ccbot import input_recovery, opencode_input
    from ccbot.bot import _messages_shared, inbound, input_feedback
    from ccbot.inbound_queue import enqueue_inbound, shutdown_inbound_queues
    from ccbot.session import Session, session_manager

    recovery = input_recovery.InputRecovery(tmp_path / "inputs.json")
    exits = scenario == "exit"
    first_payload = ".ccbot-inbox/queued.jpg" if scenario == "photo" else "first"
    recovery.bot = AsyncMock()
    recovery.bot.defaults = None
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
    ready = asyncio.Event()
    captures = asyncio.Queue()

    async def process_running(_window, *, backend):
        entry = recovery.current()
        return not exits or entry is None or entry.message_id != 1

    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", process_running
    )

    async def capture(_window):
        entry = recovery.current()
        assert entry is not None
        if entry.message_id == 1:
            captures.put_nowait(None)
        return IDLE if ready.is_set() else BLOCKED

    async def paste(_window, text, *, backend):
        assert ready.is_set() and backend == "opencode"
        if scenario == "photo" and text == first_payload:
            record_image(native_input, "photo", first_payload)
            return True
        sequence = 1 if text == first_payload else 2
        moment = 2 * sequence - 1
        native_input.execute(
            "INSERT INTO message VALUES (?, 'ses_input', ?, ?, ?)",
            (f"m{sequence}", moment, moment, json.dumps({"role": "user"})),
        )
        native_input.execute(
            "INSERT INTO part VALUES (?, ?, 'ses_input', ?, ?, ?)",
            (
                f"p{sequence}",
                f"m{sequence}",
                moment,
                moment,
                json.dumps({"type": "text", "text": text}),
            ),
        )
        native_input.execute(
            "INSERT INTO message VALUES (?, 'ses_input', ?, ?, ?)",
            (
                f"a{sequence}",
                moment + 1,
                moment + 1,
                json.dumps(
                    {
                        "role": "assistant",
                        "time": {"completed": moment + 1},
                        "finish": "stop",
                    }
                ),
            ),
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

    delivered_second = asyncio.Event()

    async def processor(update, _, wid):
        accepted, _ = await recovery.submit(wid, update.message.text, session, sender)
        if update.message.message_id == 2 and accepted:
            delivered_second.set()
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
        first, first_completion = admit(1, first_payload)
        second, second_completion = admit(2, "second")
        await asyncio.wait_for(captures.get(), 1)
        if exits:
            assert not await asyncio.wait_for(first_completion, 1)
            await recovery.deliver_notice(first)
            recovery.bot.send_message.assert_awaited_once()
            notice = recovery.bot.send_message.await_args.kwargs
            assert notice["reply_parameters"].message_id == 1
            assert "Запрос не отправлен" in notice["text"]
        else:
            # The second poll occurs after the old readiness deadline.
            await asyncio.wait_for(captures.get(), 1)
            assert not first_completion.done() and not second_completion.done()
            assert first.state == "prepared"
            recovery.bot.send_message.assert_not_awaited()
        assert native_input.execute("SELECT count(*) FROM part").fetchone()[0] == 0

        if scenario == "restart":
            await shutdown_inbound_queues()
            bot = recovery.bot
            recovery = input_recovery.InputRecovery(recovery.path)
            monkeypatch.setattr(input_recovery, "input_recovery", recovery)
            monkeypatch.setattr(inbound, "_run_text", processor)
            monkeypatch.setattr(
                input_feedback, "refresh_request_status", AsyncMock(return_value=True)
            )
            ready.set()
            recovery.start(SimpleNamespace(bot=bot))
            await asyncio.wait_for(delivered_second.wait(), 2)
            assert not recovery.records
            bot.send_message.assert_not_awaited()
        else:
            ready.set()
            if not exits:
                assert await asyncio.wait_for(first_completion, 2)
            assert await asyncio.wait_for(second_completion, 2)
            assert second.confirmed
        if scenario == "photo":
            assert first.confirmed and second.confirmed
            assert [c.args[1] for c in send.await_args_list] == [
                first_payload,
                "second",
            ]
            recovery.bot.send_message.assert_not_awaited()
            return
        parts = native_input.execute("SELECT data FROM part ORDER BY time_created")
        assert [json.loads(data)["text"] for (data,) in parts] == (
            ["second"] if exits else ["first", "second"]
        )
    finally:
        await shutdown_inbound_queues()
        await recovery.shutdown()


@pytest.mark.asyncio
async def test_open_composer_accepts_followup_during_active_tool(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    native_input.execute(
        "INSERT INTO message VALUES ('first','ses_input',1,1,?)",
        (json.dumps({"role": "user"}),),
    )
    native_input.execute(
        "INSERT INTO message VALUES ('running','ses_input',2,2,?)",
        (json.dumps({"role": "assistant", "parentID": "first"}),),
    )
    native_input.execute(
        "INSERT INTO part VALUES ('tool','running','ses_input',2,2,?)",
        (json.dumps({"type": "tool", "state": {"status": "running"}}),),
    )
    native_input.commit()
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=BUSY))
    monkeypatch.setattr(opencode_input, "READY_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )

    async def paste(_window, text, *, backend):
        assert backend == "opencode"
        native_input.execute(
            "INSERT INTO message VALUES ('follow-up','ses_input',3,3,?)",
            (json.dumps({"role": "user"}),),
        )
        native_input.execute(
            "INSERT INTO part VALUES ('follow-up','follow-up','ses_input',3,3,?)",
            (json.dumps({"type": "text", "text": text}),),
        )
        native_input.commit()
        return True

    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", paste)
    assert await tmux_input_transport.send_literal_chunked(
        "@7", "Change the current task", backend="opencode"
    )
    assert native_input.execute(
        "SELECT json_extract(data, '$.text') FROM part WHERE message_id='follow-up'"
    ).fetchall() == [("Change the current task",)]
    assert native_input.execute(
        "SELECT json_extract(data, '$.time.completed') FROM message WHERE id='running'"
    ).fetchone() == (None,)


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
    monkeypatch.setattr(recovery, "source", lambda _: SimpleNamespace())
    monkeypatch.setattr(recovery_module, "input_recovery", recovery)
    captured = asyncio.Event()
    sending = asyncio.Event()

    async def capture(window):
        captured.set()
        return IDLE if pasting else BLOCKED

    async def send():
        sending.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(opencode_input, "_capture", capture)
    monkeypatch.setattr(opencode_input, "native_checkpoint", lambda *_: None)
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


@pytest.mark.asyncio
async def test_waiting_for_opencode_keeps_previous_card_updates_visible(
    native_input, monkeypatch
):
    from ccbot import opencode_input
    from ccbot.bot import messages
    from ccbot.handlers import card_registry
    from ccbot.handlers.card_types import CardState
    from ccbot.session_models import Session

    session = Session(id="s1", name="test", window_id="@7", backend="opencode")
    state = CardState()
    manager = SimpleNamespace(
        find_session_by_window=lambda _: session,
        get_active_session=lambda _: session,
    )
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(card_registry, "session_manager", manager)
    monkeypatch.setattr(card_registry, "_repost_intent", set())
    monkeypatch.setattr(messages, "get_card_state", lambda *_: state)
    monkeypatch.setattr(messages, "is_active_for_user", lambda *_: True)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", None, raising=False)
    monkeypatch.setattr(messages, "safe_reply", AsyncMock())
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    waiting = asyncio.Event()
    ready = asyncio.Event()

    async def capture(_window):
        waiting.set()
        return IDLE if ready.is_set() else BLOCKED

    async def submit(wid, text, *_):
        ok = await tmux_input_transport.send_literal_chunked(
            wid, text, backend="opencode"
        )
        return ok, ""

    async def paste(*_, **__):
        assert card_registry._should_buffer(42, session.id, state)
        return False

    monkeypatch.setattr(messages, "submit", submit)
    monkeypatch.setattr(opencode_input, "_capture", capture)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", paste)
    task = asyncio.create_task(
        messages._dispatch_text_to_active(
            SimpleNamespace(message=SimpleNamespace(message_id=1)),
            SimpleNamespace(),
            42,
            "@7",
            "second",
            _prepared_dispatch=SimpleNamespace(text="second"),
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not card_registry._should_buffer(42, session.id, state)
        ready.set()
        assert not await asyncio.wait_for(task, 2)
        assert not card_registry._should_buffer(42, session.id, state)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def record_image(db, mid, path, caption="", *, marker="[Image 1]", session="ses_input"):
    db.execute(
        "INSERT INTO message VALUES (?,?,1,1,?)",
        (mid, session, json.dumps({"role": "user"})),
    )
    parts = [{"type": "text", "text": (caption + "\n\n" if caption else "") + marker}]
    if path:
        parts.append({"type": "file", "mime": "image/jpeg", "source": {"path": path}})
    for i, part in enumerate(parts):
        db.execute(
            "INSERT INTO part VALUES (?,?,?,1,1,?)",
            (f"{mid}-{i}", mid, session, json.dumps(part)),
        )
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "path", "stored_caption", "session", "expected"),
    [
        ("", ".ccbot-inbox/photo.jpg", "", "ses_input", True),
        ("Посмотри", ".ccbot-inbox/photo.jpg", "Посмотри", "ses_input", True),
        ("", ".ccbot-inbox/other.jpg", "", "ses_input", False),
        ("", None, "", "ses_input", False),
        ("Посмотри", ".ccbot-inbox/photo.jpg", "", "ses_input", False),
        ("", ".ccbot-inbox/photo.jpg", "", "other", False),
    ],
)
async def test_image_receipt(
    native_input, monkeypatch, caption, path, stored_caption, session, expected
):
    from ccbot import opencode_input

    photo = ".ccbot-inbox/photo.jpg"
    record_image(native_input, "old", photo, caption)

    async def paste():
        record_image(native_input, "new", path, stored_caption, session=session)
        return True

    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(opencode_input, "ACK_TIMEOUT", 0.05)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    payload = f"{caption}\n\n{photo}" if caption else photo
    assert await opencode_input.submit("@7", payload, paste) is expected


@pytest.mark.asyncio
async def test_restart_matches_late_image_and_corrects_inflight_warning(
    native_input, tmp_path, monkeypatch
):
    from ccbot import input_recovery, opencode_input
    from ccbot.bot import input_feedback

    photo = ".ccbot-inbox/late.jpg"
    recovery = input_recovery.InputRecovery(tmp_path / "inputs.json")
    request = input_recovery.InputRequest(
        "r", 42, 3, "s1", "@7", "photo", {}, prepared=photo
    )
    recovery.records["r"] = request
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    monkeypatch.setattr(recovery, "current", lambda: request)
    monkeypatch.setattr(recovery, "source", lambda _: True)
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(opencode_input, "ACK_TIMEOUT", 0.0)
    record_image(native_input, "old", photo)
    paste = AsyncMock(return_value=True)
    assert not await opencode_input.submit("@7", photo, paste)
    restored = input_recovery.InputRecovery(recovery.path)
    restored.load()
    request = restored.records["r"]
    assert request.state == "unknown" and not restored.reconcile(request)
    bot = restored.bot = AsyncMock()

    async def send_warning(**kwargs):
        record_image(native_input, "late", photo)
        assert restored.reconcile(request)
        return SimpleNamespace(message_id=53)

    bot.send_message.side_effect = send_warning
    await restored.deliver_notice(request)
    assert request.confirmed
    again = input_recovery.InputRecovery(recovery.path)
    again.load()
    again.bot = bot

    async def stop(_):
        raise asyncio.CancelledError()

    again.sleep = stop
    monkeypatch.setattr(
        input_feedback, "refresh_request_status", AsyncMock(return_value=True)
    )
    with pytest.raises(asyncio.CancelledError):
        await again.feedback_loop()
    edit = bot.edit_message_text.await_args
    assert edit and edit.kwargs["message_id"] == 53
    assert "получение агентом подтверждено" in edit.kwargs["text"]
    paste.assert_awaited_once()


def test_pre_upgrade_unknown_photo_uses_original_store_after_dispatch(
    native_input, tmp_path
):
    from ccbot.input_recovery import InputRecovery, InputRequest
    from ccbot.opencode_session_io import ensure_projection

    checkpoint = ensure_projection("ses_input", "/tmp/lab")
    photo = ".ccbot-inbox/legacy.jpg"
    journal = InputRecovery(tmp_path / "input.json")
    request = InputRequest(
        "r",
        42,
        1,
        "s1",
        "@7",
        "photo",
        {},
        state="unknown",
        checkpoint={
            "path": str(checkpoint),
            "offset": checkpoint.stat().st_size,
            "backend": "opencode",
        },
        prepared=photo,
        dispatch_started_at=1.0,
    )
    journal.records[request.id] = request
    record_image(native_input, "old", photo)
    assert not journal.reconcile(request)
    record_image(native_input, "new", photo)
    native_input.execute("UPDATE message SET time_created=1001 WHERE id='new'")
    native_input.commit()
    assert journal.reconcile(request) and request.confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["live", "restart"])
async def test_first_voice_binds_after_paste(
    native_input, tmp_path, monkeypatch, timing
):
    from ccbot import input_recovery, opencode_input
    from ccbot.config import config
    from ccbot.session import Session, session_manager

    session = Session(
        id="local",
        name="voice",
        backend="opencode",
        workdir="/tmp/lab",
        window_id="@7",
        activated_at=1,
    )
    monkeypatch.setattr(session_manager, "sessions", {session.id: session})
    recovery = input_recovery.InputRecovery(tmp_path / "inputs.json")
    spoken = "Первый голосовой запрос"
    request = input_recovery.InputRequest(
        "r", 42, 3, session.id, "@7", "voice", {}, prepared=spoken
    )
    recovery.records["r"] = request
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    monkeypatch.setattr(recovery, "current", lambda: request)
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(opencode_input, "ACK_TIMEOUT", 1 if timing == "live" else 0)
    mapping = tmp_path / "session_map.json"
    mapping.write_text("{}")
    native_input.execute(
        "INSERT INTO session VALUES ('ses_other', '/tmp/lab','other',NULL,1)"
    )

    def append(sid, mid):
        now = int(time.time() * 1000)
        native_input.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (mid, sid, now, now, json.dumps({"role": "user"})),
        )
        native_input.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (mid, mid, sid, now, now, json.dumps({"type": "text", "text": spoken})),
        )
        native_input.commit()

    def bind(window, sid):
        data = json.loads(mapping.read_text())
        data[f"{config.tmux_session_name}:{window}"] = {
            "backend": "opencode",
            "session_id": sid,
            "cwd": "/tmp/lab",
        }
        mapping.write_text(json.dumps(data))

    append("ses_input", "old")

    async def paste():
        append("ses_other", "concurrent")
        bind("@8", "ses_other")
        if timing == "live":
            append("ses_input", "first")
            bind("@7", "ses_input")
        return True

    send = AsyncMock(side_effect=paste)
    accepted = await opencode_input.submit("@7", spoken, send)
    if timing == "live":
        assert accepted
    else:
        assert not accepted
        restored = input_recovery.InputRecovery(recovery.path)
        restored.load()
        row = restored.records["r"]
        assert row.state == "unknown" and not restored.reconcile(row)
        bind("@7", "ses_input")
        session.activated_at = 2
        assert not restored.reconcile(row)  # window reclaimed after dispatch
        session.activated_at = 1
        assert not restored.reconcile(row)  # identical old message in this window
        bind("@7", "ses_other")  # a later rebind cannot redirect the receipt
        restored = input_recovery.InputRecovery(recovery.path)
        restored.load()
        row = restored.records["r"]
        assert not restored.reconcile(row)
        append("ses_input", "first")
        assert restored.reconcile(row) and row.confirmed
    send.assert_awaited_once()


@pytest.mark.parametrize(
    "first_text,confirmed", [("voice", True), ("different", False)]
)
def test_legacy_first_voice_requires_the_first_native_user_message(
    native_input, tmp_path, monkeypatch, first_text, confirmed
):
    from ccbot.input_recovery import InputRecovery, InputRequest
    from ccbot.session import Session, session_manager

    s = Session(
        id="local",
        name="voice",
        backend="opencode",
        workdir="/tmp/lab",
        window_id="@7",
        activated_at=7,
        claude_session_id="ses_input",
    )
    monkeypatch.setattr(session_manager, "sessions", {s.id: s})
    recovery = InputRecovery(tmp_path / "inputs.json")
    request = InputRequest(
        "r",
        42,
        3,
        s.id,
        "@7",
        "voice",
        {},
        prepared="voice",
        state="unknown",
        dispatch_started_at=8,
    )
    recovery.records["r"] = request
    native_input.execute(
        "ALTER TABLE session ADD COLUMN time_created INTEGER DEFAULT 8210"
    )
    for idx, value in enumerate([first_text, "voice"]):
        mid = f"m{idx}"
        native_input.execute(
            "INSERT INTO message VALUES (?, 'ses_input', ?, 1, ?)",
            (mid, 8219 + idx, json.dumps({"role": "user"})),
        )
        native_input.execute(
            "INSERT INTO part VALUES (?,?,'ses_input',1,1,?)",
            (mid, mid, json.dumps({"type": "text", "text": value})),
        )
    native_input.commit()
    assert recovery.reconcile(request) is confirmed
