"""Control remains usable during slow voice receipts and Telegram failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TimedOut

from ccbot.bot import inbound, session_events
from ccbot.bot.callbacks import switcher
from ccbot.handlers import (
    bg_status,
    card_carrier,
    card_registry,
    card_surface,
    card_updates,
)
from ccbot.handlers.card_types import CardState
from ccbot.handlers.callback_data import CB_SW_USE
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


@pytest.fixture
def setup(monkeypatch):
    sess = Session(
        id="target",
        name="ro convergence",
        window_id="@1",
        workdir="/tmp",
        backend="codex",
        claude_session_id="native",
    )
    old = Session(id="old", name="default", window_id="@2", workdir="/tmp")
    state = CardState(msg_id=100)
    manager = MagicMock()
    active = [old]
    manager.get_session.side_effect = lambda sid: sess if sid == sess.id else old
    manager.get_active_session.side_effect = lambda uid: active[0]
    manager.select_session.side_effect = lambda uid, sid: active.__setitem__(0, sess)
    manager.get_user_settings.return_value = {}
    manager.get_last_switcher_msg.return_value = 100
    manager.find_session_by_window.return_value = sess
    manager.all_user_sessions_with_claude_id.return_value = [(42, sess)]
    for module in (
        switcher,
        card_carrier,
        card_registry,
        card_surface,
        session_events,
        inbound,
    ):
        monkeypatch.setattr(module, "session_manager", manager)
    monkeypatch.setattr(
        card_registry,
        "_cards",
        {(42, sess.id): state, (42, old.id): CardState(msg_id=100)},
    )
    # Imported aliases share this exact dictionary.
    for module in (card_carrier, card_surface):
        monkeypatch.setattr(module, "_cards", card_registry._cards)
    monkeypatch.setattr(card_registry, "_carrier_edit_locks", {})
    monkeypatch.setattr(card_registry, "_card_locks", {})
    monkeypatch.setattr(bg_status, "_bg", {})
    monkeypatch.setattr(switcher, "get_card_state", lambda *a: state)
    monkeypatch.setattr(card_updates, "get_card_state", lambda *a: state)
    from ccbot.bot import _new_session_flow

    monkeypatch.setattr(_new_session_flow, "cancel_for_active_card", lambda *a: None)
    return sess, state, manager, active


@pytest.mark.asyncio
async def test_ack_timeout_recovers_card_and_buffered_final_keeps_unread_badge(
    setup, monkeypatch
):
    sess, state, manager, active = setup
    query = SimpleNamespace(
        data=CB_SW_USE + sess.id,
        message=SimpleNamespace(message_id=100),
        answer=AsyncMock(side_effect=TimedOut()),
    )
    context = SimpleNamespace(bot=AsyncMock(), user_data={})

    async def paint_card(*a, **kw):
        state.in_menu_view = False
        return True

    paint = AsyncMock(side_effect=paint_card)
    monkeypatch.setattr(
        bg_status, "infer_status_from_jsonl", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(switcher, "paint_card_on_carrier", paint)
    assert await switcher.handle(query, context, SimpleNamespace(id=42))
    assert active[0] is sess
    assert state.in_menu_view is False
    paint.assert_awaited_once()
    state.in_menu_view = True  # User deliberately opens a menu before completion.

    refresh = AsyncMock()
    monkeypatch.setattr(session_events, "is_active_for_user", lambda *a: True)
    monkeypatch.setattr(session_events, "fire_typing", AsyncMock())
    monkeypatch.setattr(session_events, "refresh_session_keyboard", refresh)
    monkeypatch.setattr(
        session_events, "context_pct_for_session", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: AsyncMock() if name == "_ensure_seeded" else None,
    )
    monkeypatch.setattr(card_updates, "_resolve_line_budget", lambda uid: 20)
    monkeypatch.setattr(card_updates, "paginate_events_for_card", lambda *a: [])
    # Real finalize_task, real lifecycle, real paused-card buffering.
    monkeypatch.setattr(session_events, "finalize_task", card_updates.finalize_task)
    await session_events.handle_new_message(
        NewMessage(
            session_id="native",
            text="done",
            is_complete=True,
            role="assistant",
            stop_reason="end_turn",
        ),
        context.bot,
    )
    assert state.events[-1].type == "final_text"
    assert bg_status.status_emoji(42, sess.id) == "✅"
    assert bg_status._bg[42][sess.id].finished_views == 0
    refresh.assert_not_awaited()
    context.bot.edit_message_reply_markup.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    # The next tap consumes the badge despite the result never being painted.
    assert not bg_status.record_finished_view(42, sess.id)
    assert bg_status.status_emoji(42, sess.id) == "✅"


@pytest.mark.asyncio
async def test_slow_keyboard_request_cannot_block_session_switch(setup, monkeypatch):
    sess, state, manager, active = setup
    active[0] = sess
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_markup(**kw):
        entered.set()
        await release.wait()
        raise TimedOut()

    monkeypatch.setattr(card_surface, "_legacy", lambda name: lambda *a, **kw: None)
    bot = SimpleNamespace(edit_message_reply_markup=delayed_markup)
    refresh = asyncio.create_task(card_surface.refresh_session_keyboard(bot, 42))
    await asyncio.wait_for(entered.wait(), 1)
    selected_before = manager.select_session.call_count
    switch = asyncio.create_task(
        card_carrier.activate_card_on_carrier(42, "old", sess.id, 100)
    )
    await asyncio.sleep(0)
    selected_during_network_wait = manager.select_session.call_count
    release.set()
    await asyncio.gather(refresh, switch, return_exceptions=True)
    assert selected_during_network_wait == selected_before + 1


@pytest.mark.asyncio
async def test_voice_receipt_does_not_wait_for_redundant_status_keyboard(
    setup, monkeypatch
):
    """Admission must send a receipt while recognition is still pending."""
    from ccbot.inbound_queue import shutdown_inbound_queues

    sess, state, manager, active = setup
    active[0] = sess
    recognizing, recognized, visible = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_keyboard = asyncio.Event()
    rendered = []

    async def slow_recognition(*args):
        recognizing.set()
        await recognized.wait()
        return True

    async def slow_keyboard(**kwargs):
        await release_keyboard.wait()
        raise TimedOut()

    async def send_receipt(bot, uid, target, card, *, text):
        rendered.append(text)
        card.msg_id = 102
        manager.get_last_switcher_msg.return_value = 102
        visible.set()
        return True

    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *a: False)
    monkeypatch.setattr(inbound, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(inbound, "active_window", lambda uid: sess.window_id)
    monkeypatch.setattr(inbound, "get_card_state", lambda *a: state)
    monkeypatch.setattr(inbound, "claim_default_session", lambda *a: None)
    monkeypatch.setattr(inbound, "_run_voice", slow_recognition)
    monkeypatch.setattr(
        inbound, "refresh_session_keyboard", card_surface.refresh_session_keyboard
    )
    monkeypatch.setattr(card_surface, "get_card_state", lambda *a: state)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "is_active_for_user": lambda *a: True,
            "build_footer_keyboard": lambda *a, **kw: None,
            "_render_card": lambda *a, **kw: (
                "voice recognizing" if state.voice_pending else "no voice"
            ),
            "_send_card": send_receipt,
            "_ensure_seeded": AsyncMock(),
        }[name],
    )
    # Keep the follow-up identity sync off the real pane.
    state.agent_model = "gpt-6.1-sol"
    state.reasoning_effort = "high"
    bot = AsyncMock()
    bot.edit_message_reply_markup.side_effect = slow_keyboard
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(voice=object(), message_id=101),
    )
    try:
        assert await inbound.voice_intake_handler(update, SimpleNamespace(bot=bot))
        await asyncio.wait_for(recognizing.wait(), 1)
        await asyncio.wait_for(visible.wait(), 0.2)
        assert not recognized.is_set()
        assert rendered == ["voice recognizing"]
        assert state.msg_id > update.message.message_id
    finally:
        release_keyboard.set()
        recognized.set()
        await shutdown_inbound_queues()
        await card_surface.shutdown_card_surface_tasks()


@pytest.mark.asyncio
async def test_new_text_shows_working_before_slow_history_seed(setup, monkeypatch):
    from ccbot.inbound_queue import shutdown_inbound_queues

    sess, state, manager, active = setup
    active[0] = sess
    bg_status.update_status(42, sess.id, "finished", force=True)
    visible, release_seed = asyncio.Event(), asyncio.Event()
    labels = []

    async def slow_seed(*_args):
        await release_seed.wait()

    async def send_card(_bot, uid, target, card, *, text):
        labels.append(bg_status.status_emoji(uid, target.id))
        card.msg_id = 102
        visible.set()
        return True

    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *a: False)
    monkeypatch.setattr(inbound, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(inbound, "active_window", lambda uid: sess.window_id)
    monkeypatch.setattr(inbound, "get_card_state", lambda *a: state)
    monkeypatch.setattr(inbound, "claim_default_session", lambda *a: None)
    monkeypatch.setattr(inbound, "_run_text", AsyncMock(return_value=True))
    monkeypatch.setattr(
        inbound, "refresh_session_keyboard", card_surface.refresh_session_keyboard
    )
    monkeypatch.setattr(card_surface, "get_card_state", lambda *a: state)
    monkeypatch.setattr(card_surface, "refresh_panel", AsyncMock())
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "is_active_for_user": lambda *a: True,
            "build_footer_keyboard": lambda *a, **kw: None,
            "_render_card": lambda *a, **kw: "receipt",
            "_send_card": send_card,
            "_ensure_seeded": slow_seed,
        }[name],
    )
    state.agent_model, state.reasoning_effort = "gpt-6.1-sol", "high"
    bot = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(text="next", message_id=101, reply_to_message=None),
    )
    try:
        assert await inbound.text_intake_handler(
            update, SimpleNamespace(bot=bot, user_data={})
        )
        await asyncio.wait_for(visible.wait(), 0.2)
        assert labels == ["🔶"]
    finally:
        release_seed.set()
        await shutdown_inbound_queues()
        await card_surface.shutdown_card_surface_tasks()


@pytest.mark.asyncio
async def test_voice_intake_returns_and_pins_before_slow_telegram_receipt(
    setup, monkeypatch
):
    sess, state, manager, active = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_surface(*a, **kw):
        entered.set()
        await release.wait()
        return True

    enqueue = MagicMock(return_value=True)
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *a: False)
    monkeypatch.setattr(inbound, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(
        inbound, "resolve_voice_backend", lambda uid: "parakeet", raising=False
    )
    monkeypatch.setattr(inbound, "active_window", lambda uid: sess.window_id)
    monkeypatch.setattr(inbound, "get_card_state", lambda *a: state)
    monkeypatch.setattr(
        inbound, "surface_card_after_message", delayed_surface, raising=False
    )
    monkeypatch.setattr(inbound, "claim_default_session", lambda *a: None)
    monkeypatch.setattr(inbound, "_enqueue", enqueue)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(voice=object(), message_id=99),
    )
    task = asyncio.create_task(
        inbound.voice_intake_handler(update, SimpleNamespace(bot=object()))
    )
    try:
        assert await asyncio.wait_for(asyncio.shield(task), 0.1)
    finally:
        release.set()
        await task
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs.get("target_window_id", "@1") == "@1"


@pytest.mark.asyncio
async def test_failed_session_paint_does_not_acknowledge_completion(setup, monkeypatch):
    sess, state, manager, active = setup
    bg_status.update_status(42, sess.id, "finished")
    bg_status.record_finished_delivery(42, sess.id)
    monkeypatch.setattr(
        bg_status, "infer_status_from_jsonl", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        switcher, "paint_card_on_carrier", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        switcher, "render_session_preview", AsyncMock(return_value="preview")
    )
    monkeypatch.setattr(switcher, "safe_edit", AsyncMock(side_effect=TimedOut()))
    monkeypatch.setattr(switcher, "build_footer_keyboard", lambda *a, **kw: None)
    monkeypatch.setattr(switcher, "is_card_busy", lambda *a: False)
    query = SimpleNamespace(
        data=CB_SW_USE + sess.id,
        message=SimpleNamespace(message_id=100),
        answer=AsyncMock(),
    )
    assert await switcher.handle(
        query, SimpleNamespace(bot=AsyncMock(), user_data={}), SimpleNamespace(id=42)
    )
    assert bg_status.status_emoji(42, sess.id) == "✅"
    assert not card_registry._switcher_carrier_is_protected(42, 100)


@pytest.mark.asyncio
async def test_switch_surfaces_selected_session_below_racing_final(setup, monkeypatch):
    sess, state, manager, active = setup
    manager.get_last_switcher_msg.return_value = 101
    monkeypatch.setattr(
        bg_status, "infer_status_from_jsonl", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(switcher, "paint_card_on_carrier", AsyncMock(return_value=True))
    surface = AsyncMock(return_value=True)
    monkeypatch.setattr(switcher, "surface_card_after_message", surface, raising=False)
    query = SimpleNamespace(
        data=CB_SW_USE + sess.id,
        message=SimpleNamespace(message_id=100),
        answer=AsyncMock(),
    )
    bot = AsyncMock()

    await switcher.handle(
        query, SimpleNamespace(bot=bot, user_data={}), SimpleNamespace(id=42)
    )

    surface.assert_awaited_once_with(
        bot, 42, sess, 101, fast=True, preserve_turn=True, delete_old=False
    )


@pytest.mark.asyncio
async def test_switch_surfaces_below_final_already_superseded(setup, monkeypatch):
    sess, state, manager, active = setup
    monkeypatch.setattr(
        switcher, "latest_registered_session_message", lambda uid, sid: 101
    )
    monkeypatch.setattr(
        bg_status, "infer_status_from_jsonl", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(switcher, "paint_card_on_carrier", AsyncMock(return_value=True))
    surface = AsyncMock(return_value=True)
    monkeypatch.setattr(switcher, "surface_card_after_message", surface)
    query = SimpleNamespace(
        data=CB_SW_USE + sess.id,
        message=SimpleNamespace(message_id=100),
        answer=AsyncMock(),
    )
    bot = AsyncMock()
    await switcher.handle(
        query, SimpleNamespace(bot=bot, user_data={}), SimpleNamespace(id=42)
    )
    surface.assert_awaited_once_with(
        bot, 42, sess, 101, fast=True, preserve_turn=True, delete_old=False
    )


@pytest.mark.asyncio
async def test_reposted_selection_preserves_turn_and_final_message(setup, monkeypatch):
    sess, state, manager, active = setup
    active[0] = sess
    state.in_menu_view = False
    sent = []
    keyboard = object()

    async def send(bot, uid, session, card, *, text, reply_markup):
        sent.append((text, reply_markup))
        card.msg_id = 102

    monkeypatch.setattr(card_surface, "get_card_state", lambda *a: state)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "is_active_for_user": lambda *a: True,
            "_render_card": lambda *a, **kw: "selected session",
            "_send_card": send,
            "build_footer_keyboard": lambda *a, **kw: keyboard,
            "_ensure_seeded": AsyncMock(),
        }[name],
    )
    state.agent_model, state.reasoning_effort = "gpt-6.1-sol", "high"
    state.events.append(SimpleNamespace(type="tool_use"))
    bot = AsyncMock()
    assert await card_surface.surface_card_after_message(
        bot, 42, sess, 101, fast=True, preserve_turn=True, delete_old=False
    )
    await card_surface.shutdown_card_surface_tasks()
    assert state.msg_id == 102
    assert sent == [("selected session", keyboard)]
    assert not any(
        call.kwargs.get("message_id") == 101
        for call in bot.delete_message.await_args_list
    )


@pytest.mark.asyncio
async def test_slow_voice_receipt_does_not_hold_session_route(setup, monkeypatch):
    sess, state, manager, active = setup
    active[0] = sess
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(*a, **kw):
        entered.set()
        await release.wait()

    monkeypatch.setattr(card_surface, "get_card_state", lambda *a: state)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {
            "is_active_for_user": lambda *a: True,
            "_render_card": lambda *a, **kw: "voice pending",
            "_send_card": slow_send,
        }[name],
    )
    receipt = card_surface.schedule_card_after_message(
        AsyncMock(), 42, sess, 101, fast=True
    )
    await asyncio.wait_for(entered.wait(), 0.2)
    switch = asyncio.create_task(
        card_carrier.activate_card_on_carrier(42, "old", sess.id, 100)
    )
    await asyncio.sleep(0)
    route_changed = manager.select_session.call_count > 0
    receipt.cancel()
    release.set()
    await asyncio.gather(receipt, switch, return_exceptions=True)
    assert route_changed
    assert state.msg_id == 100


@pytest.mark.asyncio
async def test_newest_session_selection_wins_while_old_edit_is_pending(setup):
    sess, state, manager, active = setup
    old = active[0]
    manager.select_session.side_effect = lambda uid, sid: active.__setitem__(
        0, sess if sid == sess.id else old
    )
    lock = card_registry._carrier_edit_lock(42)
    await lock.acquire()
    first = asyncio.create_task(
        card_carrier.activate_card_on_carrier(42, old.id, sess.id, 100)
    )
    await asyncio.sleep(0)
    assert active[0] is sess
    second = asyncio.create_task(
        card_carrier.activate_card_on_carrier(42, sess.id, old.id, 100)
    )
    await asyncio.sleep(0)
    assert active[0] is old
    lock.release()
    await asyncio.gather(first, second)
    assert active[0] is old
    assert state.in_menu_view is True


def test_session_switch_does_not_block_update_dispatcher():
    from telegram.ext import CallbackQueryHandler
    from ccbot.bot.app import create_bot

    app = create_bot()
    handlers = [
        h
        for group in app.handlers.values()
        for h in group
        if isinstance(h, CallbackQueryHandler)
    ]
    assert handlers[0].pattern.match(CB_SW_USE + "target")
    assert handlers[0].callback is switcher.session_switch_intake_handler
    assert handlers[0].block
    assert handlers[1].block


@pytest.mark.asyncio
async def test_switch_intake_routes_following_message_before_any_paint(
    setup, monkeypatch
):
    sess, state, manager, active = setup
    monkeypatch.setattr(switcher, "is_user_allowed", lambda uid: True)
    query = SimpleNamespace(data=CB_SW_USE + sess.id, answer=AsyncMock())
    tasks = []
    context = SimpleNamespace(
        application=SimpleNamespace(create_task=lambda coro, **kw: tasks.append(coro))
    )
    try:
        await switcher.session_switch_intake_handler(
            SimpleNamespace(
                callback_query=query, effective_user=SimpleNamespace(id=42)
            ),
            context,
        )
        assert manager.get_active_session(42) is sess
        query.answer.assert_not_awaited()
        assert len(tasks) == 1
    finally:
        for task in tasks:
            task.close()


@pytest.mark.asyncio
async def test_superseded_handoff_pauses_actual_previous_carrier_owner(setup):
    sess, state, manager, active = setup
    previous_carrier = CardState(msg_id=100)
    card_registry._cards[(42, "superseded")] = previous_carrier
    await card_carrier.activate_card_on_carrier(42, "old", sess.id, 100)
    assert previous_carrier.in_menu_view
