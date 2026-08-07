"""Regression tests for pinned, non-blocking per-session inbound delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.inbound import text_intake_handler, voice_intake_handler
from ccbot.inbound_queue import (
    enqueue_inbound,
    pending_inbound_count,
    reset_inbound_queues_for_test,
    shutdown_inbound_queues,
)


def _update(message_id: int, *, text: str | None = None, voice: bool = False):
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=42)
    update.message = SimpleNamespace(
        message_id=message_id,
        text=text,
        voice=object() if voice else None,
    )
    return update


def _context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


@pytest.fixture(autouse=True)
async def _clean_lanes():
    reset_inbound_queues_for_test()
    yield
    await shutdown_inbound_queues()


@pytest.mark.asyncio
async def test_voice_and_followup_pin_before_first_await() -> None:
    context = _context()
    voice = _update(1, voice=True)
    text = _update(2, text="follow-up")
    active = "@A"
    voice_started = asyncio.Event()
    release_voice = asyncio.Event()
    events: list[tuple[str, str]] = []

    async def run_voice(update, context, wid):
        events.append(("voice-start", wid))
        voice_started.set()
        await release_voice.wait()
        events.append(("voice-done", wid))
        return True

    async def run_text(update, context, wid):
        events.append(("text", wid))
        return True

    notice = AsyncMock()
    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", side_effect=lambda user_id: active),
        patch("ccbot.bot.inbound._run_voice", new=run_voice),
        patch("ccbot.bot.inbound._run_text", new=run_text),
        patch("ccbot.bot.inbound._queue_notice", new=notice),
    ):
        await voice_intake_handler(voice, context)
        await text_intake_handler(text, context)
        active = "@B"
        await voice_started.wait()
        await asyncio.sleep(0)
        assert events == [("voice-start", "@A")]
        notice.assert_awaited_once_with(text, context, "@A", 1)

        release_voice.set()
        while pending_inbound_count(42, "@A"):
            await asyncio.sleep(0)

    assert events == [
        ("voice-start", "@A"),
        ("voice-done", "@A"),
        ("text", "@A"),
    ]


@pytest.mark.asyncio
async def test_failed_item_does_not_stall_tail() -> None:
    context = _context()
    events: list[str] = []

    async def fail(update, context, wid):
        events.append("failed")
        return False

    async def succeed(update, context, wid):
        events.append("delivered")
        return True

    first = enqueue_inbound(
        42, "@A", _update(1, text="one"), context, kind="text", processor=fail
    )
    second = enqueue_inbound(
        42, "@A", _update(2, text="two"), context, kind="text", processor=succeed
    )

    assert await first.completion is False
    assert await second.completion is True
    assert events == ["failed", "delivered"]
    assert pending_inbound_count(42, "@A") == 0


@pytest.mark.asyncio
async def test_reply_quote_is_pinned_to_quoted_session() -> None:
    context = _context()
    update = _update(3, text="reply")
    update.message.reply_to_message = SimpleNamespace(message_id=99)
    target = SimpleNamespace(id="sessB", window_id="@B", state="active", name="B")
    seen: list[str] = []

    async def run_text(update, context, wid):
        seen.append(wid)
        return True

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@A"),
        patch("ccbot.bot.inbound.lookup_session_for_message", return_value="sessB"),
        patch("ccbot.bot.inbound.session_manager.get_session", return_value=target),
        patch("ccbot.bot.inbound._run_text", new=run_text),
    ):
        await text_intake_handler(update, context)
        while pending_inbound_count(42, "@B"):
            await asyncio.sleep(0)

    assert seen == ["@B"]


@pytest.mark.asyncio
async def test_different_session_lane_is_not_blocked_by_voice() -> None:
    context = _context()
    release_voice = asyncio.Event()
    session_b_done = asyncio.Event()

    async def slow(update, context, wid):
        await release_voice.wait()
        return True

    async def fast(update, context, wid):
        session_b_done.set()
        return True

    enqueue_inbound(
        42, "@A", _update(1, voice=True), context, kind="voice", processor=slow
    )
    enqueue_inbound(
        42, "@B", _update(2, text="new session"), context, kind="text", processor=fast
    )

    await asyncio.wait_for(session_b_done.wait(), timeout=1)
    assert pending_inbound_count(42, "@A") == 1
    release_voice.set()


def test_application_registers_fast_blocking_intake_handlers() -> None:
    from ccbot.bot.app import create_bot

    app = create_bot()
    handlers = {
        handler.callback.__name__: handler.block
        for group in app.handlers.values()
        for handler in group
        if hasattr(handler, "callback")
    }

    assert handlers["voice_intake_handler"]
    assert handlers["text_intake_handler"]
