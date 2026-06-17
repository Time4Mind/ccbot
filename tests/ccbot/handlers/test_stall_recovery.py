"""Regression tests for stall_finalize false-positive recovery.

When ``maybe_finalize_stalled`` fires too eagerly (e.g. the metrics-debug
incident on 2026-06-17: ``tail=tool_use idle=96s`` while Claude was
reasoning toward the final answer), a genuine assistant turn lands
*after* the STALL_NOTE was already appended to the card. Without the
recovery path the real reply would be silently edited into a card the
user has already scrolled past or marked as complete.

The fix: ``maybe_finalize_stalled`` sets ``state.stall_finalized=True``
after the STALL_NOTE finalize_task lands. Both ``update_session_card``
and ``finalize_task`` check the flag on entry and wipe the binding via
``_recover_from_false_stall`` so the next render lands as a fresh
``_send_card`` below the stub.
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
    _card_locks,
    _cards,
    _recover_from_false_stall,
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


def _stalled_msg(text: str = "real answer at last") -> NewMessage:
    return NewMessage(
        session_id="uuid-s1",
        text=text,
        is_complete=True,
        content_type="text",
        role="assistant",
        stop_reason="end_turn",
    )


def _seed_stalled_card(user_id: int, sess: Session, *, msg_id: int = 999) -> CardState:
    """Seed a card in the post-stall_finalize state: STALL_NOTE landed,
    flag armed, the original tool_use + STALL_NOTE final_text are in
    events. This mirrors what ``maybe_finalize_stalled`` leaves behind."""
    now = time.time()
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = msg_id
    state.events = [
        Event(type="user_msg", text="run the analysis", started_at=now - 600),
        Event(type="tool_use", text="Bash(…)", started_at=now - 500),
        Event(
            type="final_text",
            text=notifications.STALL_NOTE,
            started_at=now - 100,
        ),
    ]
    state.last_event_ts = now - 100
    state.stall_finalized = True
    return state


def test_recover_helper_wipes_binding_and_flag():
    """``_recover_from_false_stall`` clears msg_id, events, last_rendered
    AND the flag itself; sets is_continuation + clears seed_attempted so
    the next event re-pulls JSONL context."""
    state = CardState()
    state.msg_id = 7177
    state.events = [Event(type="final_text", text="stub", started_at=time.time())]
    state.last_rendered = "rendered stub"
    state.stall_finalized = True
    state.seed_attempted = True
    state.current_page_idx = 3
    state.is_continuation = False

    _recover_from_false_stall(state)

    assert state.msg_id is None
    assert state.events == []
    assert state.last_rendered == ""
    assert state.stall_finalized is False
    assert state.seed_attempted is False
    assert state.current_page_idx is None
    assert state.is_continuation is True


@pytest.mark.asyncio
async def test_update_session_card_recovers_after_stall(monkeypatch):
    """A genuine assistant turn arriving after a false stall_finalize
    must spawn a FRESH card (``_send_card``), NOT edit the stalled stub
    (``_edit_card``). The recovery path is triggered by
    ``state.stall_finalized=True``."""
    sess = _make_sess()
    bot = AsyncMock()
    sent: list[Any] = []
    edits: list[Any] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 5000 + len(sent)
        sent.append(text)

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
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0},
    )

    user_id = 42
    state = _seed_stalled_card(user_id, sess, msg_id=7177)
    assert state.stall_finalized is True
    assert state.msg_id == 7177

    await notifications.update_session_card(bot, user_id, sess, _stalled_msg())

    assert len(sent) == 1, f"expected fresh card spawn, got sends={sent} edits={edits}"
    assert edits == [], "stalled stub must not be edited"
    assert _cards[(user_id, sess.id)].stall_finalized is False


@pytest.mark.asyncio
async def test_finalize_task_recovers_after_stall(monkeypatch):
    """A real end-of-turn assistant text arriving after a false stall
    routes through ``finalize_task`` (not update_session_card). It also
    must spawn a fresh card below the stub."""
    sess = _make_sess()
    bot = AsyncMock()
    sent: list[Any] = []
    edits: list[Any] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        st.msg_id = 6000 + len(sent)
        sent.append(text)

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
    monkeypatch.setattr(
        notifications.session_manager,
        "get_user_settings",
        lambda uid: {"live_lag": 0, "card_page_lines": 45},
    )
    # ``finalize_task`` calls ``prewarm_pages_cache`` which would hit the
    # JSONL file system path — stub it to a no-op.
    fake_prewarm = AsyncMock(return_value=None)
    monkeypatch.setattr(notifications, "_send_attachments", AsyncMock())

    import ccbot.handlers.history as _history_mod

    monkeypatch.setattr(_history_mod, "prewarm_pages_cache", fake_prewarm)

    user_id = 42
    state = _seed_stalled_card(user_id, sess, msg_id=7177)
    assert state.stall_finalized is True

    await notifications.finalize_task(bot, user_id, sess, "the real BT_fin answer")

    assert len(sent) == 1, f"expected fresh card spawn, got sends={sent} edits={edits}"
    assert edits == [], "stalled stub must not be edited by finalize_task"
    assert _cards[(user_id, sess.id)].stall_finalized is False
