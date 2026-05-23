"""Regression tests for two coupled live-card bugs.

Bug 1 — *the delete+resend flicker*. ``repost_card`` (fired on every
inbound user message — the always-repost behaviour) used to refresh
``last_rendered`` / ``last_edit_ts`` but NOT ``last_event_ts``. On a card
that had been idle >= ``STALE_CARD_SECONDS``, the very next claude event
then tripped ``_is_stale`` and spawned a SECOND fresh card ~1-2s after
the repost — the user saw the card appear, vanish, and reappear. The fix
stamps ``last_event_ts`` on repost (a repost IS user activity), so the
just-reposted card can't be judged stale by the next event.

Bug 2 — *the user message rendered twice*. The stale-reset path (and the
``release_card_message`` switcher hand-off) wipes ``state.events`` and
re-seeds from JSONL — which already contains the just-submitted user
prompt — and then appends the same live event again, with no dedup. The
user's own message showed up twice in the card body. The fix guards both
append sites with ``_duplicate_of_seeded`` (match on type/started_at/text).

Both regressions were latent/visible across #88 (always-repost) and #96
(A6 page-counter re-seed); neither was introduced by the #97 refactor.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    STALE_CARD_SECONDS,
    CardState,
    Event,
    _card_locks,
    _cards,
    _duplicate_of_seeded,
    _is_stale,
    _repost_intent,
)
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


@pytest.fixture(autouse=True)
def _clear_card_state():
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
    return Event(
        type="final_text",
        text=text,
        body=text,
        started_at=now,
        completed_at=now,
        is_page_break=True,
    )


def _multi_turn_events(n: int) -> list[Event]:
    now = time.time()
    return [_final_event(f"answer {i}", now) for i in range(n)]


def _assistant_msg(text: str = "next") -> NewMessage:
    """A mid-stream assistant text event (not a page-break)."""
    return NewMessage(
        session_id="uuid-s1",
        text=text,
        is_complete=True,
        content_type="text",
        role="assistant",
        stop_reason="tool_use",
    )


def _user_msg(text: str, ts: str) -> NewMessage:
    return NewMessage(
        session_id="uuid-s1",
        text=text,
        is_complete=True,
        content_type="text",
        role="user",
        timestamp=ts,
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


def _patch_card_io(monkeypatch):
    """Stub the send/edit/seed/prewarm I/O the card path touches."""
    import ccbot.handlers.history as history_mod

    monkeypatch.setattr(history_mod, "kick_prewarm", lambda wid: None)


# ─── _duplicate_of_seeded unit ─────────────────────────────────────


def test_duplicate_of_seeded_matches_only_same_turn() -> None:
    seeded = Event(type="user_msg", text="hi", started_at=100.0, body="hi")
    events = [_final_event("ans", 99.0), seeded]

    # Same (type, started_at, text) — a re-seeded copy of the live event.
    assert _duplicate_of_seeded(
        events, Event(type="user_msg", text="hi", started_at=100.0)
    )
    # Different timestamp → a genuinely distinct turn.
    assert not _duplicate_of_seeded(
        events, Event(type="user_msg", text="hi", started_at=101.0)
    )
    # Different text.
    assert not _duplicate_of_seeded(
        events, Event(type="user_msg", text="bye", started_at=100.0)
    )
    # Different type.
    assert not _duplicate_of_seeded(
        events, Event(type="text", text="hi", started_at=100.0)
    )
    # Empty event log.
    assert not _duplicate_of_seeded(
        [], Event(type="user_msg", text="hi", started_at=100.0)
    )


# ─── Bug 1: repost resets the freshness clock ──────────────────────


@pytest.mark.asyncio
async def test_repost_resets_freshness_clock(monkeypatch):
    """A reposted card must NOT be judged stale by the next event."""
    sess = _make_sess()
    bot = AsyncMock()

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 7777

    async def fake_ensure_seeded(uid, s, st):
        return  # events already present

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    _patch_active(monkeypatch, sess)

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 5000  # old card
    state.events = _multi_turn_events(3)
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 120)
    assert _is_stale(state)  # precondition: card looks stale

    await notifications.repost_card(bot, user_id, sess)

    assert state.msg_id == 7777  # fresh card sent
    assert (time.time() - state.last_event_ts) < 5  # freshness clock reset to now
    assert not _is_stale(state)  # the just-reposted card is NOT stale


@pytest.mark.asyncio
async def test_repost_then_event_does_not_spawn_second_card(monkeypatch):
    """End-to-end of bug 1: user msg → repost → claude's first event must
    edit the reposted card in place, not spawn a second one."""
    sess = _make_sess()
    bot = AsyncMock()
    send_calls = {"n": 0}

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        send_calls["n"] += 1
        st.msg_id = 7000 + send_calls["n"]

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    async def fake_ensure_seeded(uid, s, st):
        return

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    _patch_card_io(monkeypatch)
    _patch_active(monkeypatch, sess)

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 5000
    state.events = _multi_turn_events(2)
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 120)

    await notifications.repost_card(bot, user_id, sess)
    reposted_msg_id = state.msg_id
    assert send_calls["n"] == 1  # the repost itself

    # Claude's first event arrives right after. Pre-fix it tripped _is_stale
    # (last_event_ts pinned to the old turn) and spawned a SECOND card.
    await notifications.update_session_card(
        bot, user_id, sess, _assistant_msg("first event")
    )

    assert send_calls["n"] == 1, (
        "a second card was spawned after repost (stale misfire)"
    )
    assert state.msg_id == reposted_msg_id


# ─── Bug 2: re-seed must not double-render the triggering message ──


@pytest.mark.asyncio
async def test_stale_reseed_does_not_duplicate_user_message(monkeypatch):
    """Stale card → wipe → re-seed (JSONL already holds the just-sent
    prompt) → live append. The user message must appear exactly once."""
    sess = _make_sess()
    bot = AsyncMock()
    user_msg = _user_msg("please audit the OS", "2026-05-20T10:00:00Z")

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 7200

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    async def fake_ensure_seeded(uid, s, st):
        # Mimic reading JSONL: the seed already contains the user prompt
        # that triggered this update (identical event).
        if st.events or st.seed_attempted:
            return
        st.seed_attempted = True
        st.events = [
            _final_event("previous answer", time.time()),
            notifications._build_event(user_msg),
        ]

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    _patch_card_io(monkeypatch)
    _patch_active(monkeypatch, sess)

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 6200
    state.events = _multi_turn_events(3)
    state.seed_attempted = True
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 60)  # stale → reseed

    await notifications.update_session_card(bot, user_id, sess, user_msg)

    um = [e for e in state.events if e.type == "user_msg"]
    assert len(um) == 1, f"user message duplicated: {len(um)} copies"


@pytest.mark.asyncio
async def test_distinct_event_after_reseed_is_still_appended(monkeypatch):
    """The dedup guard must not swallow a genuinely new event that the
    seed did not contain."""
    sess = _make_sess()
    bot = AsyncMock()

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 7300

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    async def fake_ensure_seeded(uid, s, st):
        if st.events or st.seed_attempted:
            return
        st.seed_attempted = True
        st.events = _multi_turn_events(2)  # seed has NO matching event

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", fake_ensure_seeded)
    _patch_card_io(monkeypatch)
    _patch_active(monkeypatch, sess)

    user_id = 42
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = 6300
    state.events = _multi_turn_events(3)
    state.seed_attempted = True
    state.last_event_ts = time.time() - (STALE_CARD_SECONDS + 60)

    await notifications.update_session_card(
        bot, user_id, sess, _assistant_msg("brand new text")
    )

    assert any(e.text == "brand new text" for e in state.events), (
        "dedup guard wrongly swallowed a genuinely new event"
    )
