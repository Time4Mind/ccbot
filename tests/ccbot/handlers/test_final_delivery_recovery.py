"""Final delivery survives transport outages independently of the live card."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError

from ccbot.handlers import card_transport, card_updates
from ccbot.handlers.card_model import CardState, Event


@pytest.mark.asyncio
async def test_final_outage_preserves_carrier_and_durable_obligation(monkeypatch):
    state = CardState(msg_id=9, events=[Event("user_msg", "request", 1.0)])
    cause = httpx.ConnectError("offline")
    error = NetworkError("connection failed")
    error.__cause__ = cause
    bot = SimpleNamespace(
        _post=AsyncMock(side_effect=error),
        edit_message_reply_markup=AsyncMock(side_effect=error),
    )
    handlers = {
        "_ensure_seeded": AsyncMock(),
        "_render_card": lambda _s, st, **_kw: "\n".join(e.text for e in st.events),
        "build_footer_keyboard": lambda *_a, **_kw: None,
        "_send_card": card_transport._send_card,
    }
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_a: False)
    monkeypatch.setattr(card_updates, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_transport, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda *_a: False)
    monkeypatch.setattr(
        card_transport, "file_button_context_for_session", AsyncMock(return_value=None)
    )
    try:
        await card_updates.finalize_task(
            bot, 42, SimpleNamespace(id="offline", window_id=""), "final answer"
        )
        assert state.msg_id == 9, (
            "The old carrier must survive an unsuccessful replacement"
        )
        from ccbot.final_delivery import final_delivery_queue

        assert final_delivery_queue.pending(42, "offline")
    finally:
        if state.pending_edit:
            state.pending_edit.cancel()


@pytest.fixture
def recovery_harness(monkeypatch):
    from ccbot.config import config
    from ccbot.handlers import card_registry
    from ccbot.session import session_manager

    state = CardState(msg_id=9, events=[Event("user_msg", "request A", 1.0)])
    session = SimpleNamespace(id="recover", window_id="")
    active = [session]
    connected = [False]
    sent = []

    async def post(endpoint, data):
        if not connected[0]:
            error = NetworkError("offline")
            error.__cause__ = httpx.ConnectError("offline")
            raise error
        sent.append((endpoint, data))
        return {
            "message_id": 10 + len(sent),
            "date": 1,
            "chat": {"id": 42, "type": "private"},
        }

    bot = SimpleNamespace(_post=post, edit_message_reply_markup=AsyncMock())
    handlers = {
        "_ensure_seeded": AsyncMock(),
        "_render_card": lambda _s, st, **_kw: "\n".join(e.text for e in st.events),
        "build_footer_keyboard": lambda *_a, **_kw: None,
        "_send_card": card_transport._send_card,
        "_edit_card": card_transport._edit_card,
    }
    monkeypatch.setattr(config, "rich_messages", True)
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(card_updates, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_transport, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda *_a: False)
    monkeypatch.setattr(
        card_transport, "file_button_context_for_session", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(card_transport, "_strip_stale_switchers", AsyncMock())
    monkeypatch.setattr(card_registry, "_cards", {(42, session.id): state})
    monkeypatch.setattr(session_manager, "get_session", lambda _sid: session)
    monkeypatch.setattr(session_manager, "get_active_session", lambda _uid: active[0])
    return SimpleNamespace(
        state=state,
        session=session,
        active=active,
        connected=connected,
        sent=sent,
        bot=bot,
    )


@pytest.mark.asyncio
async def test_real_final_transport_recovers_after_more_than_fast_retry_window(
    recovery_harness, isolated_final_deliveries
):
    from ccbot.handlers.card_final_delivery import retry_delivery

    h = recovery_harness
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    for _ in range(7):
        item = q.pending(42)[0]
        item.retry_at = 0
        assert not await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert h.state.msg_id == 9
    assert len(q.pending(42)) == 1
    assert h.state.pending_edit is None
    h.connected[0] = True
    item.retry_at = 0
    assert await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert h.state.msg_id == 11
    assert h.sent[0][0] == "sendRichMessage"
    assert "final answer A" in h.sent[0][1]["rich_message"]["markdown"]
    assert not q.pending(42)


@pytest.mark.asyncio
async def test_switch_retains_answer_and_does_not_touch_other_session(
    recovery_harness, isolated_final_deliveries
):
    from ccbot.handlers.card_final_delivery import retry_delivery

    h = recovery_harness
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    h.connected[0] = True
    h.active[0] = SimpleNamespace(id="other")
    item = q.pending(42)[0]
    item.retry_at = 0
    assert not await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert not h.sent
    assert h.state.msg_id == 9
    h.active[0] = h.session
    item.retry_at = 0
    assert await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert len(h.sent) == 1


@pytest.mark.asyncio
async def test_new_request_does_not_replace_pending_snapshot_or_lose_live_controls(
    recovery_harness, isolated_final_deliveries
):
    from ccbot.handlers.card_final_delivery import retry_delivery
    from ccbot.handlers.card_model import TurnPhase

    h = recovery_harness
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    h.state.turn_phase = TurnPhase.RUNNING
    h.state.events.append(Event("user_msg", "request B", 2.0))
    h.state.next_request_sequence += 1
    h.connected[0] = True
    item = q.pending(42)[0]
    item.retry_at = 0
    assert await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert h.state.msg_id == 9
    payload = h.sent[0][1]
    assert "final answer A" in payload["rich_message"]["markdown"]
    assert "request B" not in payload["rich_message"]["markdown"]
    assert payload.get("reply_markup") is None
    h.bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_reconstructs_final_without_new_transcript_event(
    recovery_harness, isolated_final_deliveries, monkeypatch
):
    from ccbot import final_delivery
    from ccbot.handlers.card_final_delivery import retry_delivery

    h = recovery_harness
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    restarted = final_delivery.FinalDeliveryQueue(q.path)
    monkeypatch.setattr(final_delivery, "final_delivery_queue", restarted)
    fresh_state = CardState()
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: fresh_state)
    h.connected[0] = True
    item = restarted.pending(42)[0]
    item.retry_at = 0
    assert await restarted.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert len(h.sent) == 1
    assert "final answer A" in h.sent[0][1]["rich_message"]["markdown"]
    assert not restarted.pending(42)


@pytest.mark.asyncio
async def test_switch_during_successful_send_cannot_steal_new_carrier(
    recovery_harness, isolated_final_deliveries
):
    h = recovery_harness

    async def switch_during_send(endpoint, data):
        h.active[0] = SimpleNamespace(id="other")
        h.state.in_menu_view = True
        h.state.msg_id = None
        return {"message_id": 99, "date": 1, "chat": {"id": 42, "type": "private"}}

    h.bot._post = switch_during_send
    await card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    assert h.state.msg_id is None
    card_transport._strip_stale_switchers.assert_not_awaited()
    h.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42, message_id=99, reply_markup=None
    )
    assert not isolated_final_deliveries.pending(42)


@pytest.mark.asyncio
@pytest.mark.parametrize("rich_media", [False, True])
async def test_lost_send_ack_never_creates_alternative_final_card(
    monkeypatch, rich_media
):
    from telegram.error import TimedOut
    from ccbot.config import config
    from ccbot.handlers import card_rich_media, message_sender

    monkeypatch.setattr(config, "rich_messages", True)
    bot = SimpleNamespace(
        _post=AsyncMock(side_effect=TimedOut()), send_message=AsyncMock()
    )
    with pytest.raises(TimedOut):
        if rich_media:
            await card_rich_media.send_rich_media_card(
                bot, 42, CardState(), "answer", b"png", reply_markup=None
            )
        else:
            await message_sender.send_with_fallback(bot, 42, "answer")
    assert bot._post.await_count == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_markdown_send_timeout_does_not_try_plain_duplicate(monkeypatch):
    from telegram.error import TimedOut
    from ccbot.config import config
    from ccbot.handlers.message_sender import send_with_fallback

    monkeypatch.setattr(config, "rich_messages", False)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    with pytest.raises(TimedOut):
        await send_with_fallback(bot, 42, "answer")
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_restart_during_seed_keeps_final_obligation(
    recovery_harness, isolated_final_deliveries, monkeypatch
):
    import asyncio
    from ccbot import final_delivery
    from ccbot.handlers.card_final_delivery import retry_delivery

    h = recovery_harness
    started = asyncio.Event()
    original = card_updates._legacy

    async def seed(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: seed if name == "_ensure_seeded" else original(name),
    )
    task = asyncio.create_task(
        card_updates.finalize_task(h.bot, 42, h.session, "final answer A")
    )
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    q = final_delivery.FinalDeliveryQueue(isolated_final_deliveries.path)
    assert len(q.pending(42)) == 1
    monkeypatch.setattr(final_delivery, "final_delivery_queue", q)
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: CardState())
    h.connected[0] = True
    assert await q.attempt(q.pending(42)[0], lambda item: retry_delivery(h.bot, item))
    assert "final answer A" in h.sent[0][1]["rich_message"]["markdown"]
