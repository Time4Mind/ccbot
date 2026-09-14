"""Behavioural coverage for the in-place new-session handoff."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, User

from ccbot.bot import _session_create
from ccbot.handlers.card_model import CardState
from ccbot.handlers.card_registry import _cards


@pytest.mark.asyncio
async def test_old_card_stays_background_until_atomic_new_session_handoff(
    monkeypatch,
) -> None:
    user_id = 42
    carrier_id = 100
    old_session = SimpleNamespace(id="old-session")
    new_session = SimpleNamespace(id="new-session", claude_session_id=None)
    window_state = SimpleNamespace(session_id="", cwd="", window_name="", backend="")
    entered_create = asyncio.Event()
    release_create = asyncio.Event()
    transitions: list[str] = []

    async def create_window(*_args, **_kwargs):
        entered_create.set()
        await release_create.wait()
        return True, "created", "project", "@2"

    async def atomic_handoff(*_args, **_kwargs):
        transitions.append("handoff")

    async def paint(*_args, **_kwargs):
        transitions.append("paint")

    def plain_set_active(*_args, **_kwargs):
        transitions.append("plain-set-active")

    fake_manager = SimpleNamespace(
        agent_backend="claude",
        get_active_session=lambda _uid: old_session,
        mark_window_starting=MagicMock(),
        get_window_state=lambda _wid: window_state,
        create_session=MagicMock(return_value=new_session),
        set_session_claude_id=MagicMock(),
        set_active_session=plain_set_active,
        save_state=MagicMock(),
        wait_for_session_map_entry=AsyncMock(return_value=None),
        get_user_settings=lambda _uid: {},
    )
    fake_tmux = SimpleNamespace(create_window=create_window)
    query = MagicMock(spec=CallbackQuery)
    query.message = SimpleNamespace(message_id=carrier_id)
    query.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.id = user_id
    context = SimpleNamespace(user_data={}, bot=object())
    old_state = CardState(msg_id=carrier_id, in_menu_view=True)
    _cards[(user_id, old_session.id)] = old_state

    monkeypatch.setattr(_session_create, "session_manager", fake_manager)
    monkeypatch.setattr(_session_create, "tmux_manager", fake_tmux)
    monkeypatch.setattr(
        _session_create, "activate_card_on_carrier", atomic_handoff, raising=False
    )
    monkeypatch.setattr(_session_create, "paint_card_on_carrier", paint)
    monkeypatch.setattr(
        "ccbot.startup_queue.bind_startup_queue", lambda _uid, _wid: None
    )

    task = asyncio.create_task(
        _session_create.create_and_activate_session(
            query, context, user, "/tmp/project"
        )
    )
    try:
        await asyncio.wait_for(entered_create.wait(), timeout=1)
        assert old_state.in_menu_view is True
        assert old_state.msg_id == carrier_id
        release_create.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        release_create.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _cards.pop((user_id, old_session.id), None)

    assert transitions == ["handoff", "paint"]
