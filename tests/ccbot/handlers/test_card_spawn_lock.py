"""Regression tests for Task #50 — the spawn-serialization lock.

Two concurrent paths used to be able to observe ``state.msg_id is None``
and both call ``_send_card``, producing duplicate live-card messages
that landed in chat in (sometimes) wrong order. The fix is a per-
session ``asyncio.Lock`` held across the read-decide-send window in
every function that may spawn a card.

These tests stub out the actual Telegram send via ``_send_card`` and
just count how many times it was invoked under a simulated race.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    CardState,
    _card_locks,
    _cards,
    _repost_intent,
    begin_repost_intent,
    end_repost_intent,
)
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


@pytest.fixture(autouse=True)
def _clear_card_state():
    """Reset module-level state before each test so tests are isolated."""
    _cards.clear()
    _card_locks.clear()
    _repost_intent.clear()
    yield
    _cards.clear()
    _card_locks.clear()
    _repost_intent.clear()


def _make_sess(sid: str = "s1") -> Session:
    return Session(
        id=sid,
        name="test",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="uuid-" + sid,
    )


def _make_msg(text: str = "hi") -> NewMessage:
    return NewMessage(
        session_id="uuid-s1",
        text=text,
        is_complete=True,
        content_type="text",
        role="assistant",
        stop_reason="end_turn",
    )


@pytest.mark.asyncio
async def test_concurrent_update_session_card_spawns_once(monkeypatch):
    """Two ``update_session_card`` calls firing at once must result in
    exactly one ``_send_card`` invocation; the loser sees msg_id set
    by the winner and takes the edit branch."""
    sess = _make_sess()
    bot = AsyncMock()
    sent_msg_ids: list[int] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        # Simulate latency so the second concurrent call gets a chance
        # to enter and contend.
        import asyncio

        await asyncio.sleep(0.05)
        st.msg_id = 1000 + len(sent_msg_ids)
        sent_msg_ids.append(st.msg_id)

    edits: list[Any] = []

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append(text)
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    # Skip the JSONL seeder — not relevant for the race test.
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))
    # Make active_session resolve to this session so _should_buffer
    # doesn't force the buffer-only path.
    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager,
        "get_active_session",
        lambda uid: fake_active,
    )
    # ``live_lag=0`` forces immediate edit (no coalescing deferral) so
    # the second path's edit attempt is observable synchronously.
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42

    import asyncio

    await asyncio.gather(
        notifications.update_session_card(bot, user_id, sess, _make_msg("a")),
        notifications.update_session_card(bot, user_id, sess, _make_msg("b")),
    )

    assert len(sent_msg_ids) == 1, (
        f"expected exactly 1 _send_card; got {len(sent_msg_ids)}"
    )
    assert len(edits) == 1, f"expected exactly 1 _edit_card; got {len(edits)}"


@pytest.mark.asyncio
async def test_repost_card_race_with_update_spawns_once(monkeypatch):
    """``repost_card`` (text_handler) racing with ``update_session_card``
    (claude event arriving while text_handler is mid-flight) must not
    spawn two cards."""
    sess = _make_sess()
    bot = AsyncMock()
    bot.delete_message = AsyncMock()
    sent: list[Any] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        import asyncio

        await asyncio.sleep(0.05)
        st.msg_id = 2000 + len(sent)
        sent.append(st.msg_id)

    edits: list[Any] = []

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append(text)
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))
    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager,
        "get_active_session",
        lambda uid: fake_active,
    )
    # ``live_lag=0`` forces immediate edit (no coalescing deferral) so
    # the second path's edit attempt is observable synchronously.
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42
    # Seed an existing card so repost_card has an old_msg_id to drop
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 999

    import asyncio

    await asyncio.gather(
        notifications.repost_card(bot, user_id, sess),
        notifications.update_session_card(bot, user_id, sess, _make_msg()),
    )

    assert len(sent) == 1, (
        f"expected exactly 1 _send_card across repost+update; got {len(sent)}"
    )


@pytest.mark.asyncio
async def test_serial_calls_still_spawn_then_edit(monkeypatch):
    """Sanity: non-racing serial calls — first spawns, second edits."""
    sess = _make_sess()
    bot = AsyncMock()
    sent: list[int] = []
    edits: list[Any] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 3000
        sent.append(st.msg_id)

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append(text)
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))
    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager,
        "get_active_session",
        lambda uid: fake_active,
    )
    # ``live_lag=0`` forces immediate edit (no coalescing deferral) so
    # the second path's edit attempt is observable synchronously.
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42
    await notifications.update_session_card(bot, user_id, sess, _make_msg("a"))
    await notifications.update_session_card(bot, user_id, sess, _make_msg("b"))

    assert len(sent) == 1
    assert len(edits) == 1


@pytest.mark.asyncio
async def test_finalize_then_immediate_event_no_duplicate(monkeypatch):
    """``finalize_task`` racing with the next turn's first event — the
    next event arrives while finalize is mid-render. Lock guarantees
    finalize completes (sets msg_id) before update_session_card decides."""
    sess = _make_sess()
    bot = AsyncMock()
    sent: list[int] = []
    edits: list[Any] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        import asyncio

        await asyncio.sleep(0.05)
        st.msg_id = 4000 + len(sent)
        sent.append(st.msg_id)

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append(text)
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))

    # finalize_task calls prewarm_pages_cache (async); stub it out.
    async def fake_prewarm(window_id):
        return True

    import ccbot.handlers.history as history_mod

    monkeypatch.setattr(history_mod, "prewarm_pages_cache", fake_prewarm)

    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager,
        "get_active_session",
        lambda uid: fake_active,
    )
    # ``live_lag=0`` forces immediate edit (no coalescing deferral) so
    # the second path's edit attempt is observable synchronously.
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42

    import asyncio

    await asyncio.gather(
        notifications.finalize_task(bot, user_id, sess, "final answer here"),
        notifications.update_session_card(bot, user_id, sess, _make_msg("next")),
    )

    # Exactly one card spawn; the loser took the edit branch.
    assert len(sent) == 1, f"expected 1 spawn across finalize+update; got {len(sent)}"


@pytest.mark.asyncio
async def test_repost_intent_blocks_concurrent_spawn(monkeypatch):
    """Regression for "после моего сообщения прилетает 2 от тебя".

    After a prior turn's ``finalize_task`` resets ``state.msg_id`` to
    None, the user types a follow-up. ``text_handler`` calls
    ``send_to_window`` (claude wakes up) and ``repost_card``. If claude
    emits its first reply event between those two steps, the monitor's
    ``update_session_card`` grabs the spawn lock first, sees
    ``msg_id is None`` and calls ``_send_card`` (card U). Then
    ``repost_card`` grabs the lock and calls ``_send_card`` again
    (card R). User sees both U and R when ``delete(U)`` fails — and
    loses claude's first event when ``delete(U)`` succeeds.

    Fix: ``text_handler`` sets the repost-intent flag via
    ``begin_repost_intent`` BEFORE ``send_to_window``. While the flag
    is set, ``update_session_card`` buffers events into
    ``state.events`` instead of spawning. ``repost_card`` is the only
    path that calls ``_send_card``; the buffered events drain into the
    freshly reposted card body via ``_render_card``.
    """
    sess = _make_sess()
    bot = AsyncMock()
    bot.delete_message = AsyncMock()
    sent: list[int] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        import asyncio

        await asyncio.sleep(0.05)
        st.msg_id = 5000 + len(sent)
        sent.append(st.msg_id)

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))
    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager,
        "get_active_session",
        lambda uid: fake_active,
    )
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42
    # State after finalize_task: card has events but no msg_id.
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = None

    import asyncio

    # text_handler raises the flag BEFORE send_to_window. We simulate
    # send_to_window's latency by yielding to the event loop while
    # update_session_card concurrently tries to render the first claude
    # event of the new turn.
    begin_repost_intent(user_id, sess.id)
    try:
        await asyncio.gather(
            notifications.update_session_card(bot, user_id, sess, _make_msg("evt")),
            asyncio.sleep(0),  # let update grab the lock first
        )
        await notifications.repost_card(bot, user_id, sess)
    finally:
        end_repost_intent(user_id, sess.id)

    # Exactly ONE spawn (the repost). The concurrent update_session_card
    # call buffered its event instead of spawning a duplicate card.
    assert len(sent) == 1, (
        f"expected exactly 1 _send_card (repost only); got {len(sent)} "
        "— update_session_card spawned a duplicate while repost_intent was set"
    )
    # The buffered event must still be in state.events so it renders in
    # the new card body.
    assert any(ev.text == "evt" for ev in state.events), (
        "buffered claude event was dropped instead of preserved for the next render"
    )


@pytest.mark.asyncio
async def test_repost_intent_cleared_after_text_handler(monkeypatch):
    """``end_repost_intent`` must run on every code path so the buffer
    flag doesn't persist past one user-msg dispatch.

    Without the try/finally in text_handler, an early return (e.g. when
    ``send_to_window`` fails) would leave the flag set forever and the
    live card would stay silent for that session until restart.
    """
    user_id = 7
    sess_id = "s1"
    begin_repost_intent(user_id, sess_id)
    assert (user_id, sess_id) in _repost_intent
    end_repost_intent(user_id, sess_id)
    assert (user_id, sess_id) not in _repost_intent
    # Idempotent: clearing a non-existent pair is fine.
    end_repost_intent(user_id, sess_id)
