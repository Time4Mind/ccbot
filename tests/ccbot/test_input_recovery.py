"""Crash/reconnect acceptance cases through admission, FIFO and submission."""

import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram import Update, Voice
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import Application

from ccbot import input_recovery as recovery_module
from ccbot.bot import inbound, messages
from ccbot.bot.inbound import _capture_pending_transfer
from ccbot.bot.input_feedback import refresh_request_status
from ccbot.handlers.card_types import CardState
from ccbot.inbound_queue import has_pending_inbound, shutdown_inbound_queues
from ccbot.session import Session, WindowState, session_manager


def update(number, text=None):
    message = {
        "message_id": number,
        "date": 1,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Test"},
    }
    if text is None:
        message["voice"] = {
            "file_id": f"file-{number}",
            "file_unique_id": f"unique-{number}",
            "duration": 2,
        }
    else:
        message["text"] = text
    return Update.de_json({"update_id": number, "message": message}, None)


def disconnected():
    error = NetworkError("offline")
    error.__cause__ = httpx.ConnectError("offline")
    return error


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture
async def env(monkeypatch):
    source = Session(id="source", name="source", window_id="@source")
    other = Session(id="other", name="other", window_id="@other")
    cards = defaultdict(CardState)
    monkeypatch.setattr(session_manager, "sessions", {s.id: s for s in (source, other)})
    monkeypatch.setattr(session_manager, "active_sessions", {42: source.id})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "window_states", {})
    monkeypatch.setattr(session_manager, "_resuming_windows", set())
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(
        session_manager, "get_user_settings", lambda _: {"preprocessing_mode": "off"}
    )
    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *_: False)
    monkeypatch.setattr(inbound, "get_card_state", lambda uid, sess: cards[sess.id])
    monkeypatch.setattr(inbound, "schedule_card_after_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(inbound, "refresh_session_keyboard", AsyncMock())
    recovery = recovery_module.input_recovery
    recovery.load()
    now, delays = [1000.0], []
    recovery.clock = lambda: now[0]

    async def advance(delay):
        delays.append(delay)
        now[0] += delay
        await asyncio.sleep(0)

    recovery.sleep = advance
    context = SimpleNamespace(bot=AsyncMock(), user_data={})
    yield SimpleNamespace(
        source=source,
        other=other,
        recovery=recovery,
        context=context,
        cards=cards,
        now=now,
        delays=delays,
    )
    await shutdown_inbound_queues()
    await recovery_module.input_recovery.shutdown()


@pytest.mark.asyncio
async def test_outage_keeps_fifo_other_session_and_duplicate_updates_independent(
    env, monkeypatch
):
    started, restored = asyncio.Event(), asyncio.Event()
    delivered = []

    async def process(upd, ctx, wid):
        if upd.message.message_id == 1:
            started.set()
            await restored.wait()
        delivered.append((wid, upd.message.message_id))
        return True

    monkeypatch.setattr(inbound, "_run_voice", process)
    monkeypatch.setattr(inbound, "_run_text", process)
    assert await inbound.voice_intake_handler(update(1), env.context)
    await started.wait()
    assert await inbound.text_intake_handler(update(2, "after voice"), env.context)
    assert await inbound.voice_intake_handler(update(3), env.context)
    session_manager.active_sessions[42] = "other"
    assert await inbound.voice_intake_handler(
        update(1), env.context
    )  # reconnect duplicate after switching
    assert await inbound.voice_intake_handler(update(4), env.context)
    await until(lambda: delivered == [("@other", 4)])
    restored.set()
    await until(lambda: not has_pending_inbound("@source"))
    assert delivered == [("@other", 4), ("@source", 1), ("@source", 2), ("@source", 3)]
    restarted = recovery_module.InputRecovery(env.recovery.path)
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    assert await inbound.voice_intake_handler(update(1), env.context)
    await asyncio.sleep(0)
    assert len(delivered) == 4


@pytest.mark.asyncio
async def test_restart_restores_real_update_file_and_original_fifo(env, monkeypatch):
    downloading = asyncio.Event()

    async def offline(upd, ctx, wid):
        downloading.set()
        await asyncio.Future()

    monkeypatch.setattr(inbound, "_run_voice", offline)
    assert await inbound.voice_intake_handler(update(10), env.context)
    await downloading.wait()
    assert await inbound.text_intake_handler(update(11, "follower"), env.context)
    await shutdown_inbound_queues()
    restarted = recovery_module.InputRecovery(env.recovery.path)
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    delivered = []

    async def receive(upd, ctx, wid):
        delivered.append(
            (wid, upd.message.voice.file_id if upd.message.voice else upd.message.text)
        )
        return True

    monkeypatch.setattr(inbound, "_run_voice", receive)
    monkeypatch.setattr(inbound, "_run_text", receive)
    session_manager.active_sessions[42] = "other"
    application = Application.builder().token("1:testing").build()
    restarted.start(application)
    await until(lambda: not has_pending_inbound("@source"))
    assert delivered == [("@source", "file-10"), ("@source", "follower")]
    assert restarted.duplicate(update(10)) and restarted.duplicate(
        update(11, "follower")
    )


@pytest.mark.asyncio
async def test_retry_intervals_and_retryafter_survive_restart(env, monkeypatch):
    audio = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"audio"))
    fetched = AsyncMock(side_effect=[disconnected() for _ in range(7)] + [audio])
    monkeypatch.setattr(Voice, "get_file", fetched)
    received = []

    async def process(upd, ctx, wid):
        received.append(
            await messages._download_voice_bytes(upd.message.voice, user_id=42, wid=wid)
        )
        return True

    monkeypatch.setattr(inbound, "_run_voice", process)
    assert await inbound.voice_intake_handler(update(20), env.context)
    await until(lambda: bool(received))
    assert received == [b"audio"]
    assert env.delays == [1, 2, 4, 8, 15, 15, 15]

    paused = asyncio.Event()

    async def stop_before_wait(delay):
        paused.set()
        await asyncio.Future()

    env.recovery.sleep = stop_before_wait
    monkeypatch.setattr(Voice, "get_file", AsyncMock(side_effect=RetryAfter(60)))
    assert await inbound.voice_intake_handler(update(21), env.context)
    await paused.wait()
    await shutdown_inbound_queues()
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.clock = env.recovery.clock
    resumed_delays = []

    async def resume(delay):
        resumed_delays.append(delay)
        env.now[0] += delay

    restarted.sleep = resume
    restarted.load()
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    monkeypatch.setattr(Voice, "get_file", AsyncMock(return_value=audio))
    assert await restarted.run("42:21", process, update(21), env.context, "@source")
    assert resumed_delays == [60]


@pytest.mark.asyncio
async def test_missing_source_persists_terminal_notice_until_connection_recovers(
    env, monkeypatch
):
    request = env.recovery.admit(update(30), "voice", env.source)
    session_manager.sessions.pop("source")
    processor = AsyncMock(return_value=True)
    assert not await env.recovery.run(
        request.id, processor, update(30), env.context, "@source"
    )
    processor.assert_not_awaited()
    env.recovery.bot = AsyncMock()
    env.recovery.bot.send_message.side_effect = [
        disconnected(),
        SimpleNamespace(message_id=301),
    ]
    await env.recovery.deliver_notice(request)
    assert request.id in env.recovery.records
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    restarted.bot = env.recovery.bot
    restarted.clock = lambda: request.notice_retry_at
    await restarted.deliver_notice(restarted.records[request.id])
    assert request.id not in restarted.records
    delivered = restarted.bot.send_message.call_args.kwargs["text"]
    assert "Исходная сессия" in delivered and "не перенаправлен" in delivered
    assert restarted.duplicate(update(30))


@pytest.mark.asyncio
async def test_permanent_file_error_is_not_retried_but_terminal_notice_is(
    env, monkeypatch
):
    monkeypatch.setattr(
        Voice, "get_file", AsyncMock(side_effect=BadRequest("file unavailable"))
    )

    async def process(upd, ctx, wid):
        await messages._download_voice_bytes(upd.message.voice, user_id=42, wid=wid)
        pytest.fail("permanent file error must never reach agent")

    monkeypatch.setattr(inbound, "_run_voice", process)
    assert await inbound.voice_intake_handler(update(31), env.context)
    await until(lambda: not has_pending_inbound("@source"))
    request = env.recovery.records["42:31"]
    env.recovery.bot = AsyncMock()
    env.recovery.bot.send_message.side_effect = [
        RetryAfter(45),
        disconnected(),
        SimpleNamespace(message_id=311),
    ]
    for delay in (45, 2):
        await env.recovery.deliver_notice(request)
        assert request.state == "failed"
        assert request.notice_retry_at - env.now[0] == delay
        env.now[0] += delay
    await env.recovery.deliver_notice(request)
    assert "42:31" not in env.recovery.records
    assert not request.confirmed


@pytest.mark.asyncio
async def test_ambiguous_error_notification_not_duplicated_after_restart_and_can_be_acknowledged(
    env, monkeypatch
):
    request = env.recovery.admit(update(32), "voice", env.source)
    env.recovery.fail(request, "Файл недоступен")
    env.recovery.bot = AsyncMock()
    env.recovery.bot.send_message.side_effect = NetworkError("response lost")
    await env.recovery.deliver_notice(request)
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    restarted.bot = AsyncMock()
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    await restarted.deliver_notice(restarted.records[request.id])
    restarted.bot.send_message.assert_not_awaited()
    from ccbot.bot.commands import requests

    monkeypatch.setattr(requests, "is_user_allowed", lambda _: True)
    replies = AsyncMock()
    monkeypatch.setattr(requests, "safe_reply", replies)
    await requests.requests_command(update(33, "/requests"), SimpleNamespace(args=[]))
    assert "Файл недоступен" in replies.call_args.args[1]
    await requests.requests_command(
        update(34, "/requests ack 32"), SimpleNamespace(args=["ack", "32"])
    )
    assert restarted.duplicate(update(32)) and not restarted.records


@pytest.mark.asyncio
async def test_crash_during_terminal_notice_stays_uncertain_instead_of_resending(env):
    request = env.recovery.admit(update(35), "voice", env.source)
    env.recovery.fail(request, "bad file")
    sent = asyncio.Event()

    async def lose_response(**kwargs):
        sent.set()
        await asyncio.Future()

    env.recovery.bot = SimpleNamespace(send_message=lose_response)
    task = asyncio.create_task(env.recovery.deliver_notice(request))
    await sent.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    restarted.bot = AsyncMock()
    await restarted.deliver_notice(restarted.records[request.id])
    restarted.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_agent_submission_reconciles_from_actual_appended_transcript(
    env, monkeypatch, tmp_path
):
    transcript = tmp_path / "rollout.jsonl"
    # An earlier identical message is deliberately NOT proof for this request.
    row = (
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "recognized"},
            }
        )
        + "\n"
    )
    transcript.write_text(row)
    session_manager.window_states["@source"] = WindowState(
        session_id="codex-session", backend="codex", transcript_path=str(transcript)
    )
    entered = asyncio.Event()
    sends = []

    async def sender(wid, text, sess):
        sends.append(text)
        entered.set()
        await asyncio.Future()

    async def process(upd, ctx, wid):
        await recovery_module.submit_input(wid, "recognized", env.source, sender)
        return True

    monkeypatch.setattr(inbound, "_run_voice", process)
    assert await inbound.voice_intake_handler(update(40), env.context)
    await entered.wait()
    await shutdown_inbound_queues()
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    request = restarted.records["42:40"]
    assert request.state == "unknown" and request.notice
    assert not restarted.reconcile(request)
    resumed = asyncio.create_task(
        restarted.run(request.id, process, update(40), env.context, "@source")
    )
    await asyncio.sleep(0)
    assert sends == ["recognized"] and not resumed.done()
    with transcript.open("a") as stream:
        stream.write(row)
    assert restarted.reconcile(request)
    assert await resumed
    assert sends == ["recognized"] and restarted.duplicate(update(40))


@pytest.mark.asyncio
async def test_confirmed_send_followed_by_ui_failure_never_replays(env, monkeypatch):
    sender = AsyncMock(return_value=(True, "accepted"))

    async def process(upd, ctx, wid):
        await recovery_module.submit_input(wid, "recognized", env.source, sender)
        raise NetworkError("UI is offline after input reached agent")

    request = env.recovery.admit(update(41), "voice", env.source)
    assert await env.recovery.run(
        request.id, process, update(41), env.context, "@source"
    )
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    assert restarted.duplicate(update(41)) and not restarted.records
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_recovery_feedback_coalesces_current_state_and_does_not_replay_stale_voice_status(
    env, monkeypatch
):
    from ccbot.handlers import notifications, card_surface

    state = env.cards["source"]
    state.msg_id = 100
    state.voice_pending = True
    monkeypatch.setattr(notifications, "get_card_state", lambda *_: state)
    repaint = AsyncMock(return_value=True)
    surface = AsyncMock(side_effect=disconnected())
    monkeypatch.setattr(notifications, "refresh_panel", repaint)
    monkeypatch.setattr(card_surface, "surface_card_after_message", surface)
    request = env.recovery.admit(update(101), "voice", env.source)
    assert not await refresh_request_status(env.recovery, 42, "source")
    request.recognized = "recognized"
    request.state = "prepared"
    env.recovery.complete(request)
    # Connection recovered only AFTER processing. No intermediate card or error.
    assert await refresh_request_status(env.recovery, 42, "source")
    assert not state.voice_pending
    assert surface.await_count == 1 and repaint.await_count == 1
    env.recovery.bot = AsyncMock()
    await env.recovery.deliver_notice(request)
    env.recovery.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_offline_admission_is_not_a_confirmed_agent_submission(env):
    from ccbot.remote_prompt_queue import RemotePromptQueue

    available = [False]
    queue = RemotePromptQueue(node_available=lambda _: available[0], autostart=False)
    paused = asyncio.Event()

    async def wait(delay):
        paused.set()
        await asyncio.sleep(0)

    env.recovery.sleep = wait
    sender = AsyncMock(return_value=(True, "accepted"))

    async def process(upd, ctx, wid):
        async def deliver():
            await recovery_module.submit_input(wid, "recognized", env.source, sender)
            return True

        return await queue.admit(
            original_message=upd.message,
            session_id="source",
            node_id="remote",
            node_name="remote",
            deliver=deliver,
        )

    request = env.recovery.admit(update(50), "voice", env.source)
    task = asyncio.create_task(
        env.recovery.run(request.id, process, update(50), env.context, "@source")
    )
    await paused.wait()
    assert request.id in env.recovery.records and not request.confirmed
    sender.assert_not_awaited()
    available[0] = True
    assert await task
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_real_voice_transcription_and_preprocessing_resume_without_repeating_expensive_work(
    env, monkeypatch
):
    from ccbot.bot import _messages_preprocessing as preprocessing
    from ccbot.request_preprocessing import prompt_preprocessor

    monkeypatch.setattr(messages, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(messages, "resolve_voice_backend", lambda _: "whisper")
    monkeypatch.setattr(messages, "is_active_for_user", lambda *_: False)
    monkeypatch.setattr(messages, "get_card_state", lambda *_: env.cards["source"])
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(messages, "refresh_panel", AsyncMock())
    monkeypatch.setattr(messages, "cancel_bash_capture", lambda *_: None)
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        messages.tmux_manager, "find_window_by_id", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        session_manager, "get_user_settings", lambda _: {"preprocessing_mode": "voice"}
    )
    monkeypatch.setattr(preprocessing, "get_card_state", lambda *_: env.cards["source"])
    monkeypatch.setattr(preprocessing, "is_active_for_user", lambda *_: False)
    transcription = AsyncMock(return_value="original")
    rewrite = AsyncMock(return_value="rewritten")
    monkeypatch.setattr(messages, "transcribe_voice", transcription)
    monkeypatch.setattr(prompt_preprocessor, "process", rewrite)
    audio = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(
        Voice,
        "get_file",
        AsyncMock(side_effect=[disconnected() for _ in range(4)] + [audio]),
    )
    sender = AsyncMock(return_value=(True, "accepted"))
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sender)
    session_manager._resuming_windows.add("@source")
    prepared = asyncio.Event()
    advance = env.recovery.sleep

    async def wait_for_ready(delay):
        if delay == 0.5:
            prepared.set()
            await asyncio.Future()
        await advance(delay)

    env.recovery.sleep = wait_for_ready
    assert await inbound.voice_intake_handler(update(60), env.context)
    await prepared.wait()
    sender.assert_not_awaited()
    await shutdown_inbound_queues()
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    monkeypatch.setattr(recovery_module, "input_recovery", restarted)
    session_manager._resuming_windows.clear()
    assert await restarted.run(
        "42:60", inbound._run_voice, update(60), env.context, "@source"
    )
    assert transcription.await_count == 1 and rewrite.await_count == 1
    assert sender.call_args.args[0:2] == ("@source", "rewritten")
    assert sender.await_count == 1 and restarted.duplicate(update(60))


@pytest.mark.asyncio
async def test_all_telegram_feedback_offline_then_current_receipt_only_after_success(
    env, monkeypatch
):
    from ccbot.handlers import card_surface, notifications

    state = env.cards["source"]
    monkeypatch.setattr(notifications, "get_card_state", lambda *_: state)
    receipt = AsyncMock(side_effect=disconnected())
    repaint = AsyncMock(return_value=True)
    monkeypatch.setattr(card_surface, "surface_card_after_message", receipt)
    monkeypatch.setattr(notifications, "refresh_panel", repaint)
    env.recovery.bot = AsyncMock()
    env.recovery.bot.send_message.side_effect = disconnected()
    online, waiting = asyncio.Event(), asyncio.Event()
    calls = []
    audio = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"audio"))

    async def get_file(_voice):
        calls.append("get_file")
        if not online.is_set():
            raise disconnected()
        return audio

    monkeypatch.setattr(Voice, "get_file", get_file)
    advance = env.recovery.sleep

    async def wait(delay):
        await advance(delay)
        if len(calls) == 4:
            waiting.set()
            await online.wait()

    env.recovery.sleep = wait
    delivered = []

    async def process(upd, ctx, wid):
        audio = await messages._download_voice_bytes(
            upd.message.voice, user_id=42, wid=wid
        )
        delivered.append((wid, audio))
        return True

    monkeypatch.setattr(inbound, "_run_voice", process)
    assert await inbound.voice_intake_handler(update(70), env.context)
    await waiting.wait()
    assert not await refresh_request_status(env.recovery, 42, "source")
    assert len(calls) == 4 and not delivered
    online.set()
    await until(lambda: not has_pending_inbound("@source"))
    assert delivered == [("@source", b"audio")]
    assert await refresh_request_status(env.recovery, 42, "source")
    assert not state.voice_pending and repaint.await_count == 1
    assert receipt.await_count == 1
    env.recovery.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_cannot_be_recaptured_by_new_session_transfer(env, monkeypatch):
    request = env.recovery.admit(update(71), "voice", env.source)
    env.recovery.complete(request)
    capture = AsyncMock()  # sync capture must never be entered, even after switch
    monkeypatch.setattr(inbound, "capture_transfer_message", capture)
    session_manager.active_sessions[42] = "other"
    assert _capture_pending_transfer(update(71), env.context)
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_restart_can_finish_already_rendered_known_terminal_notice(env):
    request = env.recovery.admit(update(80), "voice", env.source)
    env.recovery.fail(request, "Файл недоступен")
    request.notice_id = 800
    env.recovery.save()
    restarted = recovery_module.InputRecovery(env.recovery.path)
    restarted.load()
    restarted.bot = AsyncMock()
    restarted.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    await restarted.deliver_notice(restarted.records[request.id])
    assert restarted.duplicate(update(80)) and not restarted.records
    restarted.bot.send_message.assert_not_awaited()
