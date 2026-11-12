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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


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
        "build_footer_keyboard": lambda *_a, **_kw: InlineKeyboardMarkup(
            [[InlineKeyboardButton("Menu", callback_data="ft:menu")]]
        ),
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
    assert len(q.pending(42, include_history=True)) == 1
    h.state.current_page_idx = 1
    original = h.bot._post
    h.bot._post = AsyncMock(side_effect=NetworkError("edit failed"))
    with pytest.raises(NetworkError):
        await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert len(q.pending(42, include_history=True)) == 1
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
    assert q.pending(42, include_history=True) == [first]
    first.retry_at = 0
    await q.attempt(first, lambda row: retry_delivery(h.bot, row))
    assert q.pending(42, include_history=True) == [first]
    assert h.state.msg_id == 9
    assert h.state.current_page_idx == chosen
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
    assert h.calls[-1][1].get("reply_markup") is not None


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
    assert q.pending(42, include_history=True) == [item]
    await q.attempt(item, lambda row: retry_delivery(h.bot, row))
    assert h.state.msg_id == 9 and h.state.current_page_idx == 2
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
    assert h.calls[-1][1].get("reply_markup") is not None


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
    assert q.pending(42, include_history=True) == [first]


@pytest.mark.asyncio
async def test_open_completed_session_keeps_older_answers_in_history_without_sends(
    navigation, isolated_final_deliveries
):
    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "older final answer")
    older = q.pending(42)[0]
    h.state.events.append(Event("user_msg", "new request", 3.0, is_page_break=True))
    await card_updates.finalize_task(h.bot, 42, h.sess, "latest final answer")
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    await q.attempt(older, lambda row: retry_delivery(h.bot, row))
    assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
    assert h.state.msg_id == 9
    assert older in q.records, "An answer not displayed must not be acknowledged"
    h.state.current_page_idx = 1
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not q.records, "Opening the older answer should acknowledge that view"


@pytest.mark.asyncio
async def test_switch_intake_prevents_queue_send_before_slow_card_paint(
    navigation, isolated_final_deliveries, monkeypatch
):
    from ccbot.bot.callbacks import switcher
    from ccbot.bot import callbacks, _new_session_flow
    from ccbot.handlers.callback_data import CB_SW_USE

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "saved final answer")
    item = q.pending(42)[0]
    jobs = []
    h.state.in_menu_view = False

    def select(_user, _target):
        h.active[0] = h.sess
        jobs.append(
            asyncio.create_task(
                q.attempt(item, lambda entry: retry_delivery(h.bot, entry))
            )
        )

    def defer_paint(coro, **_kwargs):
        coro.close()

    monkeypatch.setattr(switcher, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(callbacks, "_acknowledge_completion_marker", lambda _: None)
    monkeypatch.setattr(_new_session_flow, "cancel_for_active_card", lambda *_: None)
    monkeypatch.setattr(session_manager, "select_session", select)
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=CB_SW_USE + h.sess.id, message=SimpleNamespace(message_id=9)
        ),
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(
        bot=h.bot, user_data={}, application=SimpleNamespace(create_task=defer_paint)
    )
    await switcher.session_switch_intake_handler(update, context)
    await asyncio.gather(*jobs)
    assert not h.calls, (
        "Accepting a manual selection must not release a final send before paint"
    )
    assert item in q.records


@pytest.mark.asyncio
async def test_navigation_backlog_survives_restart_and_does_not_block_new_answer(
    navigation, isolated_final_deliveries, monkeypatch
):
    from ccbot import final_delivery
    from ccbot.handlers.card_types import TurnPhase

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "older answer")
    older = q.pending(42)[0]
    h.state.events.append(Event("user_msg", "second request", 3.0, is_page_break=True))
    await card_updates.finalize_task(h.bot, 42, h.sess, "latest answer")
    h.active[0] = h.sess
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    restored = final_delivery.FinalDeliveryQueue(q.path)
    monkeypatch.setattr(final_delivery, "final_delivery_queue", restored)
    restored.start(lambda item: retry_delivery(h.bot, item))
    try:
        await asyncio.sleep(0.02)
        assert [endpoint for endpoint, _ in h.calls] == ["editMessageText"]
        assert [item.id for item in restored.pending(42, include_history=True)] == [
            older.id
        ]
        h.state.events.append(
            Event("user_msg", "third request", 5.0, is_page_break=True)
        )
        h.state.current_page_idx = None
        h.state.turn_phase = TurnPhase.RUNNING
        await card_updates.finalize_task(h.bot, 42, h.sess, "brand new answer")
        assert [endpoint for endpoint, _ in h.calls] == [
            "editMessageText",
            "sendRichMessage",
        ]
        assert h.calls[-1][1].get("reply_markup") is not None
        assert "brand new answer" in h.calls[-1][1]["rich_message"]["markdown"]
        assert [item.id for item in restored.pending(42, include_history=True)] == [
            older.id
        ]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_unknown_send_stays_unknown_in_history_until_exact_page_is_shown(
    navigation, isolated_final_deliveries, monkeypatch
):
    from ccbot import final_delivery

    h = navigation
    q = isolated_final_deliveries
    await card_updates.finalize_task(h.bot, 42, h.sess, "saved answer")
    item = q.pending(42)[0]
    item.status = "unknown"
    q.save()
    h.active[0] = h.sess
    h.state.current_page_idx = 0
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    restored = final_delivery.FinalDeliveryQueue(q.path)
    restored.load()
    monkeypatch.setattr(final_delivery, "final_delivery_queue", restored)
    saved = restored.pending(42, include_history=True)[0]
    assert saved.status == "unknown"
    assert (
        await restored.attempt(saved, lambda item: retry_delivery(h.bot, item)) is None
    )
    assert [ep for ep, _ in h.calls] == ["editMessageText"]
    h.state.current_page_idx = 1
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, h.sess, 9)
    assert not restored.records
    assert [ep for ep, _ in h.calls] == ["editMessageText", "editMessageText"]


@pytest.mark.asyncio
async def test_navigation_during_final_preparation_cannot_restore_automatic_send(
    navigation, isolated_final_deliveries, monkeypatch
):
    h = navigation
    q = isolated_final_deliveries
    entered = asyncio.Event()
    release = asyncio.Event()
    original = card_updates._legacy

    async def seed(*_args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda key: seed if key == "_ensure_seeded" else original(key),
    )
    task = asyncio.create_task(
        card_updates.finalize_task(h.bot, 42, h.sess, "saved final")
    )
    await entered.wait()
    q.defer_to_history(42, h.sess.id)
    release.set()
    await task
    h.active[0] = h.sess
    h.state.in_menu_view = False
    item = q.pending(42, include_history=True)[0]
    assert await q.attempt(item, lambda row: retry_delivery(h.bot, row)) is None
    assert not h.calls
    assert item in q.records


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_during_cleanup", [True, False])
async def test_normal_final_retains_source_link_after_handoff(
    navigation, isolated_final_deliveries, monkeypatch, switch_during_cleanup
):
    """Replay Nov12: a sent final is retired when another carrier is selected."""
    h = navigation
    h.active[0] = h.sess
    h.state.in_menu_view = False
    other = Session(id="other", name="Other", window_id="")
    pointer = [9]
    keyboards = {}
    source_text = {}
    started = asyncio.Event()
    release = asyncio.Event()
    if not switch_during_cleanup:
        release.set()
    original_post = h.bot._post

    async def post(endpoint, data):
        result = await original_post(endpoint, data)
        mid = result["message_id"]
        keyboards[mid] = data.get("reply_markup")
        source_text[mid] = data.get("rich_message", {}).get("markdown", "")
        return result

    async def edit_keyboard(**kwargs):
        if kwargs["message_id"] == 9:
            started.set()
            await release.wait()
        keyboards[kwargs["message_id"]] = kwargs["reply_markup"]

    h.bot._post = post
    h.bot.edit_message_reply_markup = edit_keyboard
    monkeypatch.setattr(
        card_transport, "_strip_stale_switchers", card_registry._strip_stale_switchers
    )
    monkeypatch.setattr(
        card_carrier, "_strip_stale_switchers", card_registry._strip_stale_switchers
    )
    monkeypatch.setattr(session_manager, "get_last_switcher_msg", lambda _: pointer[0])
    monkeypatch.setattr(
        session_manager,
        "set_last_switcher_msg",
        lambda _, mid: pointer.__setitem__(0, mid),
    )
    monkeypatch.setattr(
        session_manager,
        "get_session",
        lambda sid: {h.sess.id: h.sess, other.id: other}.get(sid),
    )
    monkeypatch.setattr(
        session_manager,
        "select_session",
        lambda _, sid: h.active.__setitem__(0, other if sid == other.id else h.sess),
    )
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    facade = {
        name: card_carrier._legacy(name)
        for name in (
            "_ensure_seeded",
            "_render_card",
            "build_footer_keyboard",
            "_edit_card",
        )
    }
    facade["is_active_for_user"] = lambda _, sess: h.active[0].id == sess.id
    monkeypatch.setattr(card_carrier, "_legacy", facade.__getitem__)
    finish = asyncio.create_task(
        card_updates.finalize_task(h.bot, 42, h.sess, "final answer")
    )
    if switch_during_cleanup:
        await asyncio.wait_for(started.wait(), 1)
    else:
        await finish
    switch = asyncio.create_task(
        card_carrier.activate_card_on_carrier(42, h.sess.id, other.id, 17)
    )
    await asyncio.sleep(0)
    assert h.active[0] is other
    release.set()
    await asyncio.wait_for(asyncio.gather(finish, switch), 2)
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, other, 17)
    assert "final answer" in source_text[100]
    assert keyboards[100] is not None, (
        "Retiring a delivered final must retain navigation"
    )
    assert keyboards[100].inline_keyboard[0][0].callback_data == "sw:target"
    assert keyboards[17].inline_keyboard[0][0].callback_data == "ft:menu"
    assert pointer[0] == 17 and not isolated_final_deliveries.pending(42)


@pytest.mark.asyncio
async def test_final_ownership_changes_only_after_successful_repaint(
    navigation, isolated_final_deliveries, monkeypatch
):
    from ccbot.bot.callbacks.switcher import _strip_orphan_switcher_if_current

    h = navigation
    queue = isolated_final_deliveries
    resolve = queue.resolve
    # Hold delivery bookkeeping while Telegram accepts a later repaint.
    monkeypatch.setattr(queue, "resolve", lambda _item: None)
    h.active[0] = h.sess
    h.state.in_menu_view = False
    await card_updates.finalize_task(h.bot, 42, h.sess, "source final")
    pointer = [100]
    monkeypatch.setattr(session_manager, "get_last_switcher_msg", lambda _: pointer[0])
    monkeypatch.setattr(
        session_manager,
        "set_last_switcher_msg",
        lambda _, mid: pointer.__setitem__(0, mid),
    )
    other = Session(id="other", name="Other", window_id="")
    h.active[0] = other
    h.state.in_menu_view = True
    facade = {
        name: card_carrier._legacy(name)
        for name in (
            "_ensure_seeded",
            "_render_card",
            "build_footer_keyboard",
            "_edit_card",
        )
    }
    facade["is_active_for_user"] = lambda _, sess: h.active[0].id == sess.id
    monkeypatch.setattr(card_carrier, "_legacy", facade.__getitem__)
    monkeypatch.setattr(
        card_carrier, "_strip_stale_switchers", card_registry._strip_stale_switchers
    )
    original = h.bot._post

    async def rejected(endpoint, data):
        raise RetryAfter(120)

    h.bot._post = rejected
    with pytest.raises(RetryAfter):
        await card_carrier.paint_card_on_carrier(h.bot, 42, other, 100)
    assert await _strip_orphan_switcher_if_current(h.bot, 42, 100)
    source_link = h.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert source_link is not None
    assert source_link.inline_keyboard[0][0].callback_data == "sw:target"

    h.bot._post = original
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, other, 100)
    item = queue.records[0]
    resolve(item)
    h.bot.edit_message_reply_markup.reset_mock()
    # The carrier now shows Other, so retiring it must not restore Target's link.
    assert await card_carrier.paint_card_on_carrier(h.bot, 42, other, 17)
    h.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42, message_id=100, reply_markup=None
    )
    assert pointer[0] == 17
