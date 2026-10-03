"""Exercise idle cleanup while real voice intake owns an aged reserve."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import default_session
from ccbot.bot import inbound, messages
from ccbot.handlers import archive
from ccbot.handlers.card_model import CardState
from ccbot.inbound_queue import pending_inbound_count, shutdown_inbound_queues
from ccbot.session import session_manager
from ccbot.session_models import Session


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["receipt", "download", "transcribe"])
async def test_first_voice_survives_idle_sweep_and_delivers_to_its_pin(
    monkeypatch, tmp_path, blocked_stage
):
    now = [200_000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    reserve = Session(
        id="claimed",
        name="default",
        window_id="@A",
        workdir=str(tmp_path),
        backend="codex",
        created_at=1,
        last_event_at=1,
        default_reserve_user_id=42,
    )
    idle = Session(id="idle", name="idle", window_id="@idle", last_event_at=1)
    unused = Session(
        id="unused",
        name="default",
        window_id="@unused",
        last_event_at=1,
        default_reserve_user_id=99,
    )
    monkeypatch.setattr(
        session_manager, "sessions", {s.id: s for s in (reserve, idle, unused)}
    )
    monkeypatch.setattr(session_manager, "active_sessions", {42: reserve.id})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "active_history", {})
    monkeypatch.setattr(
        session_manager,
        "user_settings",
        {
            42: {
                "session_idle_hours": 12,
                "default_session_enabled": True,
                "default_session_directory": str(tmp_path),
                "default_session_backend": "codex",
                "enabled_backends": ["codex"],
            }
        },
    )
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(session_manager, "mark_window_starting", lambda *_a, **_k: None)
    monkeypatch.setattr(
        default_session.tmux_manager,
        "create_window",
        AsyncMock(return_value=(True, "created", "default", "@replacement")),
    )
    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(messages, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "active_window", session_manager.get_active_window)
    monkeypatch.setattr(inbound, "resolve_voice_backend", lambda _: "whisper")
    monkeypatch.setattr(messages, "resolve_voice_backend", lambda _: "whisper")
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *_: False)
    state = CardState()
    monkeypatch.setattr(inbound, "get_card_state", lambda *_: state)
    monkeypatch.setattr(messages, "get_card_state", lambda *_: state)
    monkeypatch.setattr(inbound, "refresh_session_keyboard", AsyncMock())
    monkeypatch.setattr(
        "ccbot.handlers.notifications.refresh_session_keyboard", AsyncMock()
    )
    monkeypatch.setattr(
        messages.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@A")),
    )
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "cancel_bash_capture", lambda *_: None)
    killed = []

    async def stop(_uid, sess, _bot):
        killed.append(sess.window_id)

    monkeypatch.setattr(archive, "teardown_session_runtime", stop)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def delay(stage):
        if stage == blocked_stage:
            reached.set()
            await release.wait()

    async def receipt(*_args, **_kwargs):
        await delay("receipt")
        return True

    async def download():
        await delay("download")
        return bytearray(b"voice")

    async def transcribe(_data, **_kwargs):
        await delay("transcribe")
        return "one request"

    monkeypatch.setattr(inbound, "surface_card_after_message", receipt)
    monkeypatch.setattr(messages, "transcribe_voice", transcribe)
    delivered = []

    async def deliver(_update, _context, _uid, wid, text, **_kwargs):
        delivered.append((wid, text))
        return True

    monkeypatch.setattr(messages, "_dispatch_text_to_active", deliver)
    voice_file = SimpleNamespace(download_as_bytearray=download)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            message_id=1,
            text=None,
            caption=None,
            voice=SimpleNamespace(get_file=AsyncMock(return_value=voice_file)),
        ),
    )
    context = SimpleNamespace(bot=AsyncMock(), user_data={})
    intake = asyncio.create_task(inbound.voice_intake_handler(update, context))
    try:
        await asyncio.wait_for(reached.wait(), 1)
        # Even work lasting longer than TTL must remain protected, including
        # the first card await before the request enters the FIFO.
        now[0] += 13 * 3600
        assert await archive._archive_context_status(reserve) is not False
        await archive.idle_archive_sweep(context.bot, 42)
        assert session_manager.sessions.get(reserve.id) is reserve
        assert "@A" not in killed
        assert "@idle" in killed
        assert unused.id in session_manager.sessions
        assert not reserve.claude_session_id
        session_manager.active_sessions[42] = unused.id
        release.set()
        assert await asyncio.wait_for(intake, 1)
        while pending_inbound_count(42, "@A"):
            await asyncio.sleep(0)
        assert delivered == [("@A", "one request")]
        assert reserve.last_event_at == now[0]
        assert reserve.default_reserve_user_id == 0
        assert any(
            s.window_id == "@replacement" for s in session_manager.sessions.values()
        )
        await archive.idle_archive_sweep(context.bot, 42)
        assert reserve.id in session_manager.sessions
        now[0] += 13 * 3600
        await archive.idle_archive_sweep(context.bot, 42)
        assert reserve.id not in session_manager.sessions
    finally:
        release.set()
        await asyncio.gather(intake, return_exceptions=True)
        await shutdown_inbound_queues()
        await default_session.shutdown_default_session_tasks()
