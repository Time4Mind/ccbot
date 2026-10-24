"""Accepted Telegram voice requests recover beyond short network outages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update, Voice
from telegram.error import NetworkError

from ccbot.bot import inbound, messages
from ccbot.handlers.card_types import CardState
from ccbot.inbound_queue import has_pending_inbound, shutdown_inbound_queues
from ccbot.session import Session, session_manager


def voice_update(number=1):
    return Update.de_json(
        {
            "update_id": number,
            "message": {
                "message_id": number,
                "date": 1,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "voice": {
                    "file_id": "voice-file",
                    "file_unique_id": "unique-voice",
                    "duration": 2,
                },
            },
        },
        None,
    )


@pytest.mark.asyncio
async def test_real_intake_retries_voice_after_old_three_attempt_limit(monkeypatch):
    sess = Session(id="source", name="source", window_id="@source")
    monkeypatch.setattr(session_manager, "sessions", {sess.id: sess})
    monkeypatch.setattr(session_manager, "active_sessions", {42: sess.id})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *_: False)
    monkeypatch.setattr(inbound, "get_card_state", lambda *_: CardState())
    monkeypatch.setattr(inbound, "schedule_card_after_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(inbound, "refresh_session_keyboard", AsyncMock())
    monkeypatch.setattr(messages, "_VOICE_DOWNLOAD_RETRY_DELAYS", (0, 0))
    audio = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"voice"))
    monkeypatch.setattr(
        Voice,
        "get_file",
        AsyncMock(side_effect=[NetworkError("offline") for _ in range(3)] + [audio]),
    )
    from ccbot.input_recovery import input_recovery

    now = [1000.0]
    input_recovery.clock = lambda: now[0]

    async def skip_wait(delay):
        now[0] += delay
        await asyncio.sleep(0)

    input_recovery.sleep = skip_wait
    delivered = []

    async def process(update, context, wid):
        data = await messages._download_voice_bytes(
            update.message.voice, user_id=42, wid=wid
        )
        delivered.append((wid, data))
        return True

    monkeypatch.setattr(inbound, "_run_voice", process)
    try:
        assert await inbound.voice_intake_handler(
            voice_update(), SimpleNamespace(bot=AsyncMock(), user_data={})
        )
        async with asyncio.timeout(2):
            while has_pending_inbound("@source"):
                await asyncio.sleep(0)
        assert delivered == [("@source", b"voice")]
    finally:
        await shutdown_inbound_queues()
