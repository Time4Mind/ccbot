"""Regression tests for bug A6 — the live-card footer page counter
(``◀ N/M ▶``) transiently collapsing to ``1/1`` during active work.

``card_page_info`` derives ``M`` from ``CardState.events`` via the
turn-based ``paginate_events_for_card`` (one page per completed
``final_text`` / ``error`` page-break). The non-destructive wipe sites
that empty ``events`` mid-session — the stale-pause reset inside
``_update_session_card_locked`` and ``release_card_message`` on a
switcher hand-off — used to leave the card rebuilding one event at a
time with ``_ensure_seeded`` already spent (``seed_attempted`` stuck
True), so the counter showed ``1/1`` even though the transcript spanned
many turn-pages.

The fix keeps the counter in the card's OWN turn-based unit: those
wipe sites clear ``seed_attempted`` so the next event re-seeds the
recent transcript, and the stale-reset path additionally awaits the
re-seed inline before the body / footer is built. Counter and body
both read the same re-populated ``state.events``, so they stay in
lockstep — no borrowing of history.py's separate line-based count.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    CardState,
    Event,
    STALE_CARD_SECONDS,
    _card_locks,
    _cards,
    _repost_intent,
    card_page_info,
    release_card_message,
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


def _final_event(text: str, now: float) -> Event:
    """One completed assistant turn — a page-break anchor."""
    return Event(
        type="final_text",
        text=text,
        body=text,
        started_at=now,
        completed_at=now,
        is_page_break=True,
    )


def _multi_turn_events(n: int) -> list[Event]:
    """``n`` completed turns → ``n`` turn-based pages."""
    now = time.time()
    return [_final_event(f"answer {i}", now) for i in range(n)]


def _make_msg(text: str = "next") -> NewMessage:
    return NewMessage(
        session_id="uuid-s1",
        text=text,
        is_complete=True,
        content_type="text",
        role="assistant",
        stop_reason="tool_use",  # mid-stream event, not a page-break
    )


def _patch_active(monkeypatch, sess: Session) -> None:
    fake_active = MagicMock()
    fake_active.id = sess.id
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda uid: fake_active
    )
    monkeypatch.setattr(
        notifications.session_manager, "get_user_settings", lambda uid: {"live_lag": 0}
    )


# ─── Baseline: counter reflects the turn-based total ────────────────


def test_card_page_info_counts_completed_turns() -> None:
    """A healthy card with several completed turns reports total > 1."""
    state = CardState()
    state.events = _multi_turn_events(4)
    _idx, total = card_page_info(state, user_id=1)
    assert total == 4


def test_empty_card_reports_single_page() -> None:
    """Genuinely empty transcript → ``1`` (no false inflation)."""
    state = CardState()
    _idx, total = card_page_info(state, user_id=1)
    assert total == 1


# ─── The bug: stale-reset re-seeds instead of collapsing to 1/1 ─────


@pytest.mark.asyncio
async def test_stale_reset_reseeds_so_counter_does_not_collapse(monkeypatch):
    """A stale active card wipes ``events`` then re-seeds the recent
    transcript inline, so the footer total stays multi-page instead of
    dropping to ``1/1`` while the session keeps working."""
    sess = _make_sess()
    bot = AsyncMock()

    captured: dict[str, int] = {}

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 7000
        # Snapshot what the footer counter would show at send time.
        _idx, captured["total_at_send"] = card_page_info(st, uid)

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    # The JSONL re-seed: when ``seed_attempted`` is freshly cleared and
    # events are empty, repopulate with the recent multi-turn transcript.
    async def fake_ensure_seeded(uid, s, st):
        if st.events:
            return
        if st.seed_attempted:
            return
        st.seed_attempted = True
        st.events = _multi_turn_events(5)

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    import ccbot.handlers.history as history_mod

    monkeypatch.setattr(history_mod, "kick_prewarm", lambda wid: None)
    _patch_active(monkeypatch, sess)

    user_id = 42
    # Pre-existing card that has already been seeded once and gone stale:
    # msg_id set, last_event_ts well past STALE_CARD_SECONDS.
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 6000
    state.events = _multi_turn_events(3)
    state.seed_attempted = True
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 60)

    await notifications.update_session_card(bot, user_id, sess, _make_msg("evt"))

    # The fresh card spawned (stale → msg_id cleared → _send_card).
    assert state.msg_id == 7000
    # Counter must NOT have collapsed to 1: the re-seed restored the
    # recent transcript (5 turns) and the new in-flight event rides the
    # latest page → total stays > 1.
    assert captured["total_at_send"] > 1, (
        f"footer collapsed to {captured['total_at_send']}/1 on stale reset"
    )
    # Body and counter agree because both read the same state.events.
    _idx, total_now = card_page_info(state, user_id)
    assert total_now == captured["total_at_send"]


@pytest.mark.asyncio
async def test_stale_reset_clears_seed_flag(monkeypatch):
    """The stale-reset path must clear ``seed_attempted`` so the inline
    re-seed is allowed to fire (the gate that previously blocked it)."""
    sess = _make_sess()
    bot = AsyncMock()
    seed_calls: list[bool] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 7100

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    async def fake_ensure_seeded(uid, s, st):
        # Record the flag value seen on each call; only re-seed once.
        seed_calls.append(st.seed_attempted)
        if st.events or st.seed_attempted:
            return
        st.seed_attempted = True
        st.events = _multi_turn_events(2)

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    import ccbot.handlers.history as history_mod

    monkeypatch.setattr(history_mod, "kick_prewarm", lambda wid: None)
    _patch_active(monkeypatch, sess)

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 6100
    state.events = _multi_turn_events(3)
    state.seed_attempted = True
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 60)

    await notifications.update_session_card(bot, user_id, sess, _make_msg("evt"))

    # The second _ensure_seeded (inside the stale-reset block) must have
    # observed seed_attempted=False — i.e. the reset re-armed seeding.
    assert seed_calls[-1] is False, (
        "stale reset did not clear seed_attempted; re-seed would be blocked"
    )


# ─── release_card_message re-arms seeding for the next card ─────────


def test_release_card_message_clears_seed_flag() -> None:
    """A switcher hand-off releases the carrier and empties events, but
    the session keeps running — the next event's fresh card must be
    allowed to re-seed (so its counter reflects the real transcript)."""
    user_id = 42
    sess = _make_sess()
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 8000
    state.events = _multi_turn_events(4)
    state.seed_attempted = True

    release_card_message(user_id, sess.id)

    assert state.events == []
    assert state.msg_id is None
    assert state.seed_attempted is False, (
        "release_card_message left seeding spent; the fresh card cannot re-seed"
    )


# ─── Negative: /clear is an intentional wipe-to-zero ────────────────


@pytest.mark.asyncio
async def test_clear_card_keeps_seed_spent(monkeypatch):
    """``/clear`` is a deliberate wipe-to-zero — it must NOT re-arm
    seeding (the card stays empty until new events arrive)."""
    sess = _make_sess()
    bot = AsyncMock()

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(
        notifications,
        "build_footer_keyboard",
        lambda *a, **k: None,
    )

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 9000
    state.events = _multi_turn_events(3)
    state.seed_attempted = True

    captured_flag: dict[str, Any] = {}

    # clear_card calls reset_card at the end (pops the state). Snapshot
    # the flag just before the events are wiped by recording it on the
    # state object the edit path renders.
    orig_render = notifications._render_card

    def spy_render(s, st, *, footer="", user_id=None):
        captured_flag["seed_attempted"] = st.seed_attempted
        captured_flag["events_len"] = len(st.events)
        return orig_render(s, st, footer=footer, user_id=user_id)

    monkeypatch.setattr(notifications, "_render_card", spy_render)

    await notifications.clear_card(bot, user_id, sess)

    # At render time the events were already wiped to zero...
    assert captured_flag["events_len"] == 0
    # ...but seeding was NOT re-armed — /clear means "stay empty".
    assert captured_flag["seed_attempted"] is True
