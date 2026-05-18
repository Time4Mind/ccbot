"""Regression tests for kb-mode surfacing when the carrier is on a menu.

Bug: when the user has the live card on Menu / List / Settings /
History (``state.in_menu_view=True``) and claude emits an interactive
prompt (AskUserQuestion / ExitPlanMode / permission), the kb-mode
keyboard never appeared until the user tapped Shot. Root cause:
``enter_kb_mode`` → ``_edit_card`` short-circuits with ``return True``
when ``state.in_menu_view`` is set, leaving the menu screen visible.
Shot → ``close_card_view`` dropped ``msg_id=None`` so the next poll
cycle went through ``_send_card`` instead, finally spawning the kb
card.

Fix: ``enter_kb_mode`` clears ``in_menu_view`` before painting so
``_edit_card`` actually edits; ``_should_buffer`` then keeps
``in_kb_mode`` as a buffer reason so stray streaming events don't
overwrite the kb keyboard before the user acts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    CardState,
    _card_locks,
    _cards,
    _repost_intent,
    _should_buffer,
    enter_kb_mode,
)
from ccbot.session_models import Session


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


@pytest.mark.asyncio
async def test_enter_kb_mode_clears_menu_view(monkeypatch):
    """``enter_kb_mode`` MUST clear ``in_menu_view`` before editing the
    card, otherwise ``_edit_card`` short-circuits and the kb keyboard
    never surfaces — the user previously had to tap Shot to unstick it.
    """
    sess = _make_sess()
    bot = AsyncMock()
    edits: list[dict] = []
    sends: list[dict] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        sends.append({"text": text, "reply_markup": reply_markup})
        st.msg_id = 1234

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append({"text": text, "reply_markup": reply_markup})
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)

    # Carrier exists AND user is on a Menu screen — the exact failure
    # configuration the user reported.
    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = 999
    state.in_menu_view = True

    await enter_kb_mode(bot, 42, sess, "Which file should I edit?", "AskUserQuestion")

    assert state.in_menu_view is False, (
        "in_menu_view must be cleared on kb-mode entry — otherwise _edit_card "
        "short-circuits and the kb keyboard never appears"
    )
    assert state.in_kb_mode is True
    assert state.kb_prompt == "Which file should I edit?"
    assert len(edits) == 1, "kb-mode entry must edit the existing carrier"
    assert len(sends) == 0, "should edit, not spawn a new card"
    # And the keyboard passed to _edit_card must be the kb keyboard,
    # NOT the default footer.
    assert edits[0]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_enter_kb_mode_spawns_when_no_carrier(monkeypatch):
    """Sanity: when ``msg_id is None`` (e.g. after Shot's
    ``close_card_view``), ``enter_kb_mode`` spawns via ``_send_card``.
    This is the previously-only-working path that masked the bug.
    """
    sess = _make_sess()
    bot = AsyncMock()
    sends: list[dict] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        sends.append({"text": text, "reply_markup": reply_markup})
        st.msg_id = 5678

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)

    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = None
    state.in_menu_view = True  # close_card_view leaves this set

    await enter_kb_mode(bot, 42, sess, "Choose option:", "AskUserQuestion")

    assert len(sends) == 1
    assert state.msg_id == 5678
    assert state.in_kb_mode is True


def test_should_buffer_blocks_on_in_kb_mode():
    """Once kb-mode is active, regular claude events must buffer so
    ``update_session_card`` doesn't repaint over the kb keyboard with
    the default footer.
    """
    state = CardState()
    state.in_kb_mode = True
    state.in_menu_view = False  # menu wasn't the trigger
    fake_active = MagicMock()
    fake_active.id = "s1"
    import ccbot.session as session_mod

    original = session_mod.session_manager.get_active_session
    session_mod.session_manager.get_active_session = lambda uid: fake_active
    try:
        assert _should_buffer(42, "s1", state) is True
    finally:
        session_mod.session_manager.get_active_session = original


def test_should_buffer_does_not_block_when_idle():
    """Sanity: with no menu / no kb / no repost intent and session is
    active, _should_buffer returns False (the normal render path).
    """
    state = CardState()
    fake_active = MagicMock()
    fake_active.id = "s1"
    import ccbot.session as session_mod

    original = session_mod.session_manager.get_active_session
    session_mod.session_manager.get_active_session = lambda uid: fake_active
    try:
        assert _should_buffer(42, "s1", state) is False
    finally:
        session_mod.session_manager.get_active_session = original
