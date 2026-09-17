"""Regression tests for pinned, non-blocking per-session inbound delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.inbound import text_intake_handler, voice_intake_handler
from ccbot.handlers.card_model import CardState
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

    surface = MagicMock()
    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", side_effect=lambda user_id: active),
        patch("ccbot.bot.inbound._run_voice", new=run_voice),
        patch("ccbot.bot.inbound._run_text", new=run_text),
        patch("ccbot.bot.inbound.schedule_card_after_message", new=surface),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            side_effect=lambda wid: SimpleNamespace(id=wid),
        ),
    ):
        await voice_intake_handler(voice, context)
        await text_intake_handler(text, context)
        active = "@B"
        await voice_started.wait()
        await asyncio.sleep(0)
        assert events == [("voice-start", "@A")]
        assert [call.args[3] for call in surface.call_args_list] == [1, 2]

        release_voice.set()
        while pending_inbound_count(42, "@A"):
            await asyncio.sleep(0)

    assert events == [
        ("voice-start", "@A"),
        ("voice-done", "@A"),
        ("text", "@A"),
    ]


@pytest.mark.asyncio
async def test_voice_intake_marks_recognition_before_fifo_runs() -> None:
    context = _context()
    voice = _update(11, voice=True)
    sess = SimpleNamespace(id="s1")
    state = SimpleNamespace(voice_pending=False, current_page_idx=3)
    observed: list[tuple[bool, object]] = []

    def surface(_bot, _uid, _sess, _message_id):
        observed.append((state.voice_pending, state.current_page_idx))

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@A"),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch("ccbot.bot.inbound.get_card_state", return_value=state, create=True),
        patch("ccbot.bot.inbound.schedule_card_after_message", side_effect=surface),
        patch("ccbot.bot.inbound.enqueue_inbound"),
    ):
        assert await voice_intake_handler(voice, context)

    assert observed == [(True, None)]


@pytest.mark.asyncio
async def test_text_intake_focuses_latest_before_delayed_monitor_event() -> None:
    context = _context()
    text = _update(12, text="next request")
    sess = SimpleNamespace(id="s1")
    state = SimpleNamespace(voice_pending=False, current_page_idx=3)
    observed: list[object] = []

    def surface(_bot, _uid, _sess, _message_id):
        observed.append(state.current_page_idx)

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@A"),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch("ccbot.bot.inbound.get_card_state", return_value=state, create=True),
        patch("ccbot.bot.inbound.schedule_card_after_message", side_effect=surface),
        patch("ccbot.bot.inbound.enqueue_inbound"),
    ):
        assert await text_intake_handler(text, context)

    assert observed == [None]


@pytest.mark.asyncio
async def test_two_intakes_keep_ordered_focus_receipts_until_transcript() -> None:
    context = _context()
    first = _update(21, text="request A")
    second = _update(22, text="request B")
    sess = SimpleNamespace(id="s1")
    state = CardState(current_page_idx=3)
    release = asyncio.Event()

    async def blocked_processor(_update, _context, _wid):
        await release.wait()
        return True

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@A"),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch("ccbot.bot.inbound.get_card_state", return_value=state),
        patch("ccbot.bot.inbound._run_text", new=blocked_processor),
        patch("ccbot.bot.inbound.schedule_card_after_message"),
    ):
        assert await text_intake_handler(first, context)
        assert await text_intake_handler(second, context)
        assert state.current_page_idx is None
        assert state.pending_request_sequences == [(21, 1), (22, 2)]
        release.set()
        while pending_inbound_count(42, "@A"):
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failed_intake_drops_only_its_focus_receipt() -> None:
    context = _context()
    failed = _update(31, text="failed")
    sess = SimpleNamespace(id="s1")
    state = CardState()

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@A"),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch("ccbot.bot.inbound.get_card_state", return_value=state),
        patch("ccbot.bot.inbound._run_text", new=AsyncMock(return_value=False)),
        patch("ccbot.bot.inbound.schedule_card_after_message"),
    ):
        assert await text_intake_handler(failed, context)
        while pending_inbound_count(42, "@A"):
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert state.pending_request_sequences == []


@pytest.mark.asyncio
async def test_reserve_is_claimed_before_request_enters_fifo() -> None:
    context = _context()
    update = _update(13, text="first request")
    sess = SimpleNamespace(id="reserve")
    state = SimpleNamespace(voice_pending=False, current_page_idx=None)
    order: list[str] = []

    with (
        patch("ccbot.bot.inbound.is_user_allowed", return_value=True),
        patch("ccbot.bot.inbound.active_window", return_value="@R"),
        patch(
            "ccbot.bot.inbound.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch("ccbot.bot.inbound.get_card_state", return_value=state),
        patch("ccbot.bot.inbound.schedule_card_after_message"),
        patch(
            "ccbot.bot.inbound.claim_default_session",
            side_effect=lambda *_args: order.append("claim"),
            create=True,
        ),
        patch(
            "ccbot.bot.inbound.enqueue_inbound",
            side_effect=lambda *_args, **_kwargs: order.append("enqueue"),
        ),
    ):
        assert await text_intake_handler(update, context)

    assert order == ["claim", "enqueue"]


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
        patch("ccbot.bot.inbound.schedule_card_after_message"),
    ):
        await text_intake_handler(update, context)
        while pending_inbound_count(42, "@B"):
            await asyncio.sleep(0)

    assert seen == ["@B"]


@pytest.mark.asyncio
async def test_directory_name_does_not_enqueue_or_surface_active_card() -> None:
    context = _context()
    context.user_data = {"state": "naming_directory"}
    update = _update(4, text="test")
    target = AsyncMock(return_value=True)

    with (
        patch("ccbot.bot.inbound.text_handler", new=target),
        patch("ccbot.bot.inbound._enqueue") as enqueue,
        patch("ccbot.bot.inbound.schedule_card_after_message") as surface,
    ):
        assert await text_intake_handler(update, context)

    target.assert_awaited_once_with(update, context)
    enqueue.assert_not_called()
    surface.assert_not_called()


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
    from ccbot.telegram_rate_limit import PersistentEndpointRateLimiter

    app = create_bot()
    assert isinstance(app.bot.rate_limiter, PersistentEndpointRateLimiter)
    handlers = {
        handler.callback.__name__: handler.block
        for group in app.handlers.values()
        for handler in group
        if hasattr(handler, "callback")
    }

    assert handlers["voice_intake_handler"]
    assert handlers["text_intake_handler"]
    commands = {
        command
        for group in app.handlers.values()
        for handler in group
        for command in getattr(handler, "commands", ())
    }
    assert "screenshot" not in commands


def test_application_records_every_user_message_before_flow_capture() -> None:
    from ccbot.bot.app import create_bot

    app = create_bot()

    callbacks = [handler.callback.__name__ for handler in app.handlers[-2]]
    assert callbacks == ["record_user_message_activity"]


@pytest.mark.asyncio
async def test_ordered_voice_uses_intake_card_as_its_only_receipt() -> None:
    from ccbot.bot import inbound

    target = AsyncMock(return_value=True)
    with patch("ccbot.bot.inbound.voice_handler", new=target):
        await inbound._run_voice(_update(1, voice=True), _context(), "@A")

    target.assert_awaited_once()
    assert target.await_args.kwargs == {
        "pinned_wid": "@A",
        "ordered": True,
        "surface_pending": False,
    }
