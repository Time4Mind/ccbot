"""Navigation confirms the exact displayed final without sending another card."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.config import config
from ccbot.handlers import (
    card_carrier,
    card_registry,
    card_terminal,
    card_transport,
    card_updates,
)
from ccbot.handlers.card_types import CardState, Event
from ccbot.handlers.card_final_delivery import retry_delivery
from ccbot.session import Session
from ccbot.session import session_manager
from telegram.error import RetryAfter


@pytest.fixture
def navigation(monkeypatch):
    sess = Session(id="target", name="target", window_id="")
    state = CardState(
        msg_id=9, events=[Event("user_msg", "request", 1.0)], in_menu_view=True
    )
    active = [SimpleNamespace(id="other")]
    calls = []

    async def post(endpoint, data):
        calls.append((endpoint, data))
        return {
            "message_id": data.get("message_id", 100),
            "date": 1,
            "chat": {"id": 42, "type": "private"},
        }

    bot = SimpleNamespace(_post=post, edit_message_reply_markup=AsyncMock())
    monkeypatch.setattr(card_registry, "_card_locks", {})
    monkeypatch.setattr(card_registry, "_carrier_edit_locks", {})
    monkeypatch.setattr(card_registry, "_user_send_locks", {})
    monkeypatch.setattr(config, "rich_messages", True)
    monkeypatch.setattr(card_terminal, "sync_card_identity", AsyncMock())
    monkeypatch.setattr(session_manager, "get_active_session", lambda _uid: active[0])
    monkeypatch.setattr(session_manager, "get_session", lambda _sid: sess)
    monkeypatch.setattr(card_registry, "_cards", {(42, sess.id): state})
    monkeypatch.setattr(card_carrier, "_cards", card_registry._cards)
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_a: state)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda *_a: False)
    monkeypatch.setattr(
        card_transport, "file_button_context_for_session", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(card_transport, "_strip_stale_switchers", AsyncMock())
    monkeypatch.setattr(card_carrier, "_strip_stale_switchers", AsyncMock())
    from ccbot.handlers.card_model import _render_card
    from ccbot.handlers.card_registry import _register_msg

    handlers = {
        "_ensure_seeded": AsyncMock(),
        "_render_card": _render_card,
        "build_footer_keyboard": lambda *_a, **_kw: None,
        "_send_card": card_transport._send_card,
        "_edit_card": card_transport._edit_card,
        "is_active_for_user": lambda *_a: active[0].id == sess.id,
    }
    monkeypatch.setattr(card_updates, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_carrier, "_legacy", handlers.__getitem__)
    monkeypatch.setattr(card_transport, "_legacy", handlers.__getitem__)
    _register_msg(42, 9, sess.id)
    return SimpleNamespace(sess=sess, state=state, active=active, calls=calls, bot=bot)


@pytest.mark.asyncio
async def test_rate_limited_final_send_automatically_updates_current_card(
    navigation, isolated_final_deliveries
):
    h = navigation
    h.active[0] = h.sess
    h.state.in_menu_view = False
    original = h.bot._post

    async def post(endpoint, data):
        if endpoint == "sendRichMessage":
            h.calls.append((endpoint, data))
            raise RetryAfter(14460)
        return await original(endpoint, data)

    h.bot._post = post
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    assert [endpoint for endpoint, _ in h.calls] == [
        "sendRichMessage",
        "editMessageText",
    ]
    assert "final answer" in h.calls[-1][1]["rich_message"]["markdown"]
    assert h.state.msg_id == 9
    assert not isolated_final_deliveries.pending(42)


@pytest.mark.asyncio
async def test_rate_limited_send_and_edit_preserve_pending_final(
    navigation, isolated_final_deliveries
):
    h = navigation
    h.active[0] = h.sess
    h.state.in_menu_view = False

    async def post(endpoint, data):
        h.calls.append((endpoint, data))
        raise RetryAfter(120 if endpoint == "sendRichMessage" else 30)

    h.bot._post = post
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    assert [endpoint for endpoint, _ in h.calls] == [
        "sendRichMessage",
        "editMessageText",
    ]
    assert h.state.msg_id == 9
    assert len(isolated_final_deliveries.pending(42)) == 1
    assert isolated_final_deliveries.pending(42)[0].cooldown_until >= time.time() + 115


@pytest.mark.asyncio
async def test_rate_limited_send_does_not_repaint_after_session_switch(
    navigation, isolated_final_deliveries
):
    h = navigation
    h.active[0] = h.sess
    h.state.in_menu_view = False

    async def post(endpoint, data):
        h.calls.append((endpoint, data))
        h.active[0] = SimpleNamespace(id="other")
        raise RetryAfter(120)

    h.bot._post = post
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    assert [endpoint for endpoint, _ in h.calls] == ["sendRichMessage"]
    assert len(isolated_final_deliveries.pending(42)) == 1


@pytest.mark.asyncio
async def test_background_final_open_is_one_edit_and_no_additional_send(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
    assert not q.pending(42)
    assert "ожидает доставки" not in h.calls[0][1]["rich_message"]["markdown"]
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert all(endpoint == "editMessageText" for endpoint, _ in h.calls)


@pytest.mark.asyncio
async def test_other_page_and_failed_edit_do_not_confirm_final(
    navigation, isolated_final_deliveries
):
    from telegram.error import NetworkError

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    h.active[0] = h.sess
    h.state.current_page_idx = 0
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert len(q.pending(42)) == 1
    h.state.current_page_idx = 1
    original = h.bot._post
    h.bot._post = AsyncMock(side_effect=NetworkError("edit failed"))
    with pytest.raises(NetworkError):
        await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert len(q.pending(42)) == 1
    h.bot._post = original
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not q.pending(42)


@pytest.mark.asyncio
async def test_multiple_same_text_finals_only_acknowledge_selected_turn(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "same answer")
    first = q.pending(42)[0]
    h.state.events.append(Event("user_msg", "next request", 3.0, is_page_break=True))
    await card_updates.finalize_task(h.bot, 42, h.sess, "same answer")
    h.active[0] = h.sess
    chosen = h.state.current_page_idx
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert q.pending(42) == [first]
    first.retry_at = 0
    await q.attempt(first, lambda row: retry_delivery(h.bot, row))
    assert not q.pending(42)
    assert h.state.msg_id == 9
    assert h.state.current_page_idx == chosen
    assert [endpoint for endpoint, _ in h.calls] == [
        "editMessageText",
        "sendRichMessage",
    ]
    assert h.calls[-1][1].get("reply_markup") is None


@pytest.mark.asyncio
async def test_restart_after_navigation_receipt_cannot_replay_final(
    navigation, isolated_final_deliveries, monkeypatch
):
    from ccbot import final_delivery

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(
        h.bot, 42, h.sess, "final answer", event_key="unique-turn"
    )
    item = q.pending(42)[0]
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    restarted = final_delivery.FinalDeliveryQueue(q.path)
    monkeypatch.setattr(final_delivery, "final_delivery_queue", restarted)
    assert not restarted.pending(42)
    assert restarted.contains_event("unique-turn")
    assert await restarted.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]


@pytest.mark.asyncio
async def test_restart_seed_matches_exact_request_when_legacy_final_time_differs(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    h.state.events = [
        Event("user_msg", "request", 1.0),
        Event("final_text", "final answer", 2.0, body="final answer", completed_at=2.0),
    ]
    h.state.current_page_idx = None
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not q.pending(42)
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]


@pytest.mark.asyncio
async def test_navigation_edit_wins_race_with_waiting_queue_send(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    h.active[0] = h.sess
    edit_started = asyncio.Event()
    release = asyncio.Event()
    original = h.bot._post

    async def controlled(endpoint, data):
        if endpoint == "editMessageText":
            edit_started.set()
            await release.wait()
        return await original(endpoint, data)

    h.bot._post = controlled
    paint = asyncio.create_task(
        card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    )
    await asyncio.wait_for(edit_started.wait(), 1)
    worker = asyncio.create_task(
        q.attempt(item, lambda row: retry_delivery(h.bot, row))
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(paint, worker), 2)
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
    assert not q.pending(42)


@pytest.mark.asyncio
async def test_navigation_waits_for_an_already_started_send(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    h.active[0] = h.sess
    h.state.in_menu_view = False
    started = asyncio.Event()
    release = asyncio.Event()
    original = h.bot._post

    async def controlled(endpoint, data):
        if endpoint == "sendRichMessage":
            started.set()
            await release.wait()
        return await original(endpoint, data)

    h.bot._post = controlled
    worker = asyncio.create_task(
        q.attempt(item, lambda row: retry_delivery(h.bot, row))
    )
    await asyncio.wait_for(started.wait(), 1)
    paint = asyncio.create_task(
        card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(worker, paint), 2)
    assert [endpoint for endpoint, _ in h.calls] == [
        "sendRichMessage",
        "editMessageText",
    ]
    assert not q.pending(42)


@pytest.mark.asyncio
async def test_new_request_preserves_page_and_controls_during_older_retry(
    navigation, isolated_final_deliveries
):
    from ccbot.handlers.card_types import TurnPhase

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    h.active[0] = h.sess
    h.state.events.append(Event("user_msg", "newer request", 3.0, is_page_break=True))
    h.state.current_page_idx = 2
    h.state.turn_phase = TurnPhase.RUNNING
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert q.pending(42) == [item]
    await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert h.state.msg_id == 9 and h.state.current_page_idx == 2
    assert (
        h.calls[-1][0] == "sendRichMessage"
        and h.calls[-1][1].get("reply_markup") is None
    )


@pytest.mark.asyncio
async def test_buffering_does_not_accumulate_network_attempts_or_warnings(
    navigation, isolated_final_deliveries, caplog
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    for _ in range(4):
        assert await q.attempt(item, lambda row: retry_delivery(h.bot, row)) is None
    assert item.attempts == 0
    assert not h.calls
    assert not any(
        "final_delivery_pending" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_explicit_successful_view_reconciles_unknown_send(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    item = q.pending(42)[0]
    item.status = "unknown"
    q.save()
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not q.pending(42)
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]


@pytest.mark.asyncio
async def test_minimal_journal_matches_transcript_after_restart_during_seed(
    navigation, isolated_final_deliveries, monkeypatch
):
    h = navigation
    q = isolated_final_deliveries
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
        card_updates.finalize_task(
            h.bot, 42, h.sess, "final answer", source_timestamp="1970-01-01T00:00:02Z"
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(q.pending(42)) == 1
    h.state.events = [
        Event("user_msg", "request", 1.0),
        Event("final_text", "final answer", 2.0, body="final answer", completed_at=2.0),
    ]
    h.state.current_page_idx = None
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not q.pending(42)
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]


@pytest.mark.asyncio
async def test_same_final_text_and_timestamp_still_require_correct_request(
    navigation, isolated_final_deliveries, monkeypatch
):
    h = navigation
    q = isolated_final_deliveries
    monkeypatch.setattr(card_updates.time, "time", lambda: 10.0)
    await card_updates.finalize_task(h.bot, 42, h.sess, "same answer")
    first = q.pending(42)[0]
    h.state.events.append(
        Event("user_msg", "different request", 3.0, is_page_break=True)
    )
    await card_updates.finalize_task(h.bot, 42, h.sess, "same answer")
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert q.pending(42) == [first]
