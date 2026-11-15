"""Public recovery behavior for a failed archive restore."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from telegram import Update
from ccbot import input_recovery as recovery_module
from ccbot import input_startup
from ccbot.session_models import Session


def update(mid, voice=False):
    msg = {
        "message_id": mid,
        "date": 1,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Test"},
    }
    msg.update(
        {"voice": {"file_id": "voice-file", "file_unique_id": "v", "duration": 1}}
        if voice
        else {"text": "next message"}
    )
    return Update.de_json({"update_id": mid, "message": msg}, None)


async def test_failed_restore_keeps_unsubmitted_fifo_after_notice_and_restart(
    tmp_path, monkeypatch
):
    r = recovery_module.InputRecovery(tmp_path / "requests.json")
    monkeypatch.setattr(recovery_module, "input_recovery", r)
    sess = Session(id="old", name="old", window_id="@old")
    a = r.admit(update(1, True), "voice", sess)
    b = r.admit(update(2), "text", sess)
    a.recognized = "recognized speech"
    a.prepared = "prepared speech"
    a.state = "prepared"
    uncertain = r.admit(update(3), "text", sess)
    uncertain.state = "unknown"
    uncertain.dispatch_started_at = 10
    r.save()
    input_startup.defer_session(sess.id, "@old")
    r.bot = AsyncMock()
    r.bot.send_message.return_value = SimpleNamespace(message_id=100)
    await r.deliver_notice(a)
    await r.deliver_notice(b)
    reloaded = recovery_module.InputRecovery(r.path)
    reloaded.load()
    assert list(reloaded.records) == [a.id, b.id, uncertain.id]
    assert reloaded.records[a.id].recognized == "recognized speech"
    assert reloaded.records[a.id].startup_deferred
    assert reloaded.records[b.id].startup_deferred
    assert not reloaded.records[uncertain.id].startup_deferred
    assert reloaded.records[uncertain.id].state == "unknown"


async def test_restore_failure_notifies_even_without_queued_messages(monkeypatch):
    from ccbot.handlers import message_sender

    send = AsyncMock()
    monkeypatch.setattr(message_sender, "safe_send", send)
    sess = Session(id="old", name="machine hourly", state="archived")
    await input_startup.notify_restore_failure(
        AsyncMock(), 42, sess, RuntimeError("startup timeout")
    )
    text = send.await_args.args[2]
    assert "machine hourly" in text
    assert "восстановить" in text
    assert "startup timeout" in text


async def test_startup_admission_survives_restart_and_resumes_fifo_in_original_session(
    tmp_path, monkeypatch
):
    import asyncio
    from telegram.ext import Application
    from ccbot.bot import inbound
    from ccbot.session import session_manager
    from ccbot.inbound_queue import has_pending_inbound, shutdown_inbound_queues

    r = recovery_module.InputRecovery(tmp_path / "requests.json")
    monkeypatch.setattr(recovery_module, "input_recovery", r)
    sess = Session(id="old", name="old", window_id="", state="archived")
    context = SimpleNamespace(
        application=Application.builder().token("1:testing").build()
    )
    entries = [
        SimpleNamespace(update=update(11, True), context=context),
        SimpleNamespace(update=update(12), context=context),
    ]
    for entry in entries:
        input_startup.admit_entry(entry, sess)
    reloaded = recovery_module.InputRecovery(r.path)
    reloaded.load()
    assert all(x.state == "waiting_session" for x in reloaded.records.values())
    monkeypatch.setattr(recovery_module, "input_recovery", reloaded)
    sess.window_id = "@new"
    sess.state = "active"
    monkeypatch.setattr(
        session_manager, "get_session", lambda sid: sess if sid == "old" else None
    )
    monkeypatch.setattr(session_manager, "is_window_starting", lambda wid: False)
    seen = []

    async def receive(upd, ctx, wid):
        seen.append((upd.message.message_id, wid))
        return True

    monkeypatch.setattr(inbound, "_run_voice", receive)
    monkeypatch.setattr(inbound, "_run_text", receive)
    input_startup.resume_deferred(sess, context, [])
    input_startup.resume_deferred(
        sess, context, []
    )  # repeated restore must not duplicate
    async with asyncio.timeout(2):
        while has_pending_inbound("@new"):
            await asyncio.sleep(0)
    assert seen == [(11, "@new"), (12, "@new")]
    assert reloaded.records == {}
    await shutdown_inbound_queues()


def test_late_restore_failure_keeps_user_selected_other_session():
    manager = SimpleNamespace(
        active_sessions={42: "new-choice"},
        active_sessions_by_node={42: {"local": "new-choice"}},
        active_history={42: ["old-choice"]},
    )
    input_startup.rollback_selection(
        manager, 42, "failed", "local", "prior", "prior", ["older"]
    )
    assert manager.active_sessions[42] == "new-choice"
    assert manager.active_sessions_by_node[42]["local"] == "new-choice"
    assert manager.active_history[42] == ["old-choice"]


async def test_cancelled_saved_request_is_not_replayed_on_restore(
    tmp_path, monkeypatch
):
    from ccbot.bot.commands import requests

    r = recovery_module.InputRecovery(tmp_path / "requests.json")
    monkeypatch.setattr(recovery_module, "input_recovery", r)
    monkeypatch.setattr(requests, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(requests, "safe_reply", AsyncMock())
    sess = Session(id="old", name="old", window_id="@old")
    req = r.admit(update(21), "text", sess)
    input_startup.defer_session(sess.id)
    await requests.requests_command(update(22), SimpleNamespace(args=["cancel", "21"]))
    assert not req.startup_deferred
    assert not req.media_deferred
