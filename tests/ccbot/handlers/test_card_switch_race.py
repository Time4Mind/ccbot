"""Regression test for an old-session edit racing a switcher hand-off."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import (
    card_rich_media,
    card_transport,
    message_sender,
    notifications,
)
from ccbot.bot import _common
from ccbot.handlers.card_binding import bind_carrier
from ccbot.handlers.card_model import CardState
from ccbot.handlers.card_types import CarrierKind
from ccbot.session import session_manager
from ccbot.session_models import Session


@pytest.fixture(autouse=True)
def _clean_state():
    saved_sessions = dict(session_manager.sessions)
    saved_active = dict(session_manager.active_sessions)
    saved_history = {
        user_id: list(history)
        for user_id, history in session_manager.active_history.items()
    }
    notifications._cards.clear()
    notifications._card_locks.clear()
    notifications._carrier_edit_locks.clear()
    try:
        session_manager.sessions.clear()
        session_manager.active_sessions.clear()
        session_manager.active_history.clear()
        yield
    finally:
        notifications._cards.clear()
        notifications._card_locks.clear()
        notifications._carrier_edit_locks.clear()
        session_manager.sessions.clear()
        session_manager.sessions.update(saved_sessions)
        session_manager.active_sessions.clear()
        session_manager.active_sessions.update(saved_active)
        session_manager.active_history.clear()
        session_manager.active_history.update(saved_history)


def _session(session_id: str, name: str, window_id: str) -> Session:
    return Session(
        id=session_id,
        name=name,
        window_id=window_id,
        workdir=f"/tmp/{name}",
        state="active",
    )


@pytest.mark.asyncio
async def test_atomic_handoff_persists_card_and_active_session_once(monkeypatch):
    """One switch checkpoint must contain both carrier and routing changes."""
    user_id = 42
    carrier_msg_id = 8000
    old = _session("session-a", "old-session", "@1")
    target = _session("session-b", "target-session", "@2")
    session_manager.sessions.update({old.id: old, target.id: target})
    session_manager.active_sessions[user_id] = old.id
    notifications._cards[(user_id, old.id)] = CardState(msg_id=carrier_msg_id)
    notifications._cards[(user_id, target.id)] = CardState(msg_id=7000)

    saves: list[None] = []
    monkeypatch.setattr(session_manager, "save_state", lambda: saves.append(None))

    orphan_msg_id = await notifications.activate_card_on_carrier(
        user_id,
        old.id,
        target.id,
        carrier_msg_id,
    )

    assert orphan_msg_id == 7000
    assert session_manager.get_card_msg(user_id) == carrier_msg_id
    assert session_manager.get_active_session(user_id) is target
    assert len(saves) == 1


@pytest.mark.asyncio
async def test_inflight_old_edit_finishes_before_target_owns_carrier(monkeypatch):
    """A delayed edit from A must complete before B is activated and painted."""
    user_id = 42
    carrier_msg_id = 8000
    old = _session("session-a", "old-session", "@1")
    target = _session("session-b", "target-session", "@2")
    session_manager.sessions.update({old.id: old, target.id: target})
    session_manager.active_sessions[user_id] = old.id

    old_state = CardState(msg_id=carrier_msg_id)
    target_state = CardState(msg_id=7000)
    notifications._cards[(user_id, old.id)] = old_state
    notifications._cards[(user_id, target.id)] = target_state

    old_edit_started = asyncio.Event()
    release_old_edit = asyncio.Event()
    rendered: list[str] = []

    async def controlled_rich_edit(
        bot, chat_id, message_id, text, *, reply_markup=None, file_base_dir=None
    ):
        rendered.append(text)
        if text == "old-session late update":
            old_edit_started.set()
            await release_old_edit.wait()
        return True

    monkeypatch.setattr(message_sender, "try_rich_edit", controlled_rich_edit)
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    bot = AsyncMock()

    old_edit = asyncio.create_task(
        notifications._edit_card(
            bot,
            user_id,
            old_state,
            text="old-session late update",
        )
    )
    await old_edit_started.wait()

    hand_off = asyncio.create_task(
        notifications.activate_card_on_carrier(
            user_id,
            old.id,
            target.id,
            carrier_msg_id,
        )
    )
    await asyncio.sleep(0)

    # The switch cannot claim the carrier while A's Telegram edit is in flight.
    assert not hand_off.done()
    assert session_manager.get_active_session(user_id) is old

    release_old_edit.set()
    await old_edit
    orphan_msg_id = await hand_off

    assert orphan_msg_id == 7000
    assert session_manager.get_active_session(user_id) is target
    assert old_state.in_menu_view is True
    assert target_state.in_menu_view is True

    await notifications.paint_card_on_carrier(
        bot,
        user_id,
        target,
        carrier_msg_id,
    )

    # The only edit after the delayed A update belongs to B, so B wins.
    assert len(rendered) == 2
    assert rendered[0] == "old-session late update"
    assert "target-session" in rendered[1]
    assert target_state.in_menu_view is False


@pytest.mark.asyncio
async def test_navigation_view_wins_over_inflight_card_edit(monkeypatch):
    """A tap must be the last writer even when a live-card edit is in flight."""
    user_id = 42
    carrier_msg_id = 8000
    sess = _session("session-a", "old-session", "@1")
    session_manager.sessions[sess.id] = sess
    session_manager.active_sessions[user_id] = sess.id
    state = CardState(msg_id=carrier_msg_id)
    notifications._cards[(user_id, sess.id)] = state

    card_edit_started = asyncio.Event()
    release_card_edit = asyncio.Event()
    rendered: list[str] = []

    async def controlled_rich_edit(
        bot, chat_id, message_id, text, *, reply_markup=None, file_base_dir=None
    ):
        card_edit_started.set()
        await release_card_edit.wait()
        rendered.append(text)
        return True

    async def navigation_edit(query, text, *, reply_markup=None):
        rendered.append(text)
        return True

    monkeypatch.setattr(message_sender, "try_rich_edit", controlled_rich_edit)
    monkeypatch.setattr(_common, "safe_edit", navigation_edit)
    query = SimpleNamespace(message=SimpleNamespace(message_id=carrier_msg_id))
    bot = AsyncMock()

    card_edit = asyncio.create_task(
        notifications._edit_card(bot, user_id, state, text="late card")
    )
    await card_edit_started.wait()
    navigation = asyncio.create_task(
        _common.set_view(query, bot, user_id, "menu", reply_markup=None)
    )
    await asyncio.sleep(0)

    assert not navigation.done()
    release_card_edit.set()
    await asyncio.gather(card_edit, navigation)
    assert rendered == ["late card", "menu"]


@pytest.mark.asyncio
async def test_late_resume_cannot_reclaim_background_carrier(monkeypatch):
    """A voice dispatch that resumes after hand-off must leave the old
    session paused and must not edit the carrier now owned by the target."""
    user_id = 42
    carrier_msg_id = 8000
    old = _session("session-a", "old-session", "@1")
    target = _session("session-b", "target-session", "@2")
    session_manager.sessions.update({old.id: old, target.id: target})
    session_manager.active_sessions[user_id] = target.id

    old_state = CardState(msg_id=carrier_msg_id, in_menu_view=True)
    notifications._cards[(user_id, old.id)] = old_state
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    rich_edit = AsyncMock(return_value=True)
    monkeypatch.setattr(message_sender, "try_rich_edit", rich_edit)

    await notifications.resume_card_view(AsyncMock(), user_id, old)

    assert old_state.in_menu_view is True
    assert old_state.msg_id == carrier_msg_id
    rich_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_from_new_session_flow_repaints_rich_card(monkeypatch):
    """Back must restore the card even when its cached text/pane are unchanged."""
    user_id = 42
    carrier_msg_id = 8001
    active = _session("session-a", "active-session", "@1")
    session_manager.sessions[active.id] = active
    session_manager.active_sessions[user_id] = active.id

    state = CardState(
        in_menu_view=True,
        last_rendered="restored card",
    )
    bind_carrier(
        state,
        carrier_msg_id,
        CarrierKind.RICH_MEDIA,
        rich_media_file_id="cached-pane",
        pane_hash="same-pane",
        photo_edit_ts=1.0,
    )
    notifications._cards[(user_id, active.id)] = state
    notifications._register_msg(user_id, carrier_msg_id, active.id)

    edit = AsyncMock(return_value=None)
    capture = AsyncMock(return_value=(b"same", "same-pane"))
    monkeypatch.setattr(
        notifications, "_render_card", lambda *_args, **_kwargs: "restored card"
    )
    monkeypatch.setattr(
        notifications,
        "build_footer_keyboard",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(card_rich_media.rich, "edit_rich_message", edit)
    monkeypatch.setattr(card_rich_media, "_capture_pane_png", capture)
    monkeypatch.setattr(card_rich_media.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: True)

    await notifications.resume_card_view(AsyncMock(), user_id, active)

    edit.assert_awaited_once()
    assert edit.await_args.args[3] == "restored card"
