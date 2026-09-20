"""Behavioural coverage for the in-place new-session handoff."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, User

from ccbot.bot import _session_create
from ccbot.handlers.card_model import CardState
from ccbot.handlers.card_registry import _cards
from ccbot.startup_queue import begin_startup_queue, has_startup_queue


@pytest.mark.asyncio
async def test_slow_remote_creation_returns_control_to_telegram_immediately(
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    received: list[tuple[object, ...]] = []
    new_session = SimpleNamespace(id="remote-session", claude_session_id=None)

    async def create_session(*_args, **_kwargs):
        received.append(_args)
        entered.set()
        await release.wait()
        return {
            "ok": True,
            "target_window_id": "@8",
            "target_workdir": "/worker/project",
            "target_agent_session_id": "agent-8",
        }

    runtime = SimpleNamespace(create_session=create_session)
    fake_manager = SimpleNamespace(
        agent_backend="claude",
        get_active_session=lambda _uid: None,
        create_session=MagicMock(return_value=new_session),
        set_session_claude_id=MagicMock(),
        set_active_session=MagicMock(),
        save_state=MagicMock(),
    )
    query = MagicMock(spec=CallbackQuery)
    query.message = None
    query.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.id = 42
    context = SimpleNamespace(
        user_data={
            "_new_session_backend": "claude",
            "_pending_session_name": "Original task",
        },
        bot=object(),
    )
    monkeypatch.setattr(_session_create, "session_manager", fake_manager)
    monkeypatch.setattr(_session_create, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(
        "ccbot.startup_queue.bind_startup_queue", lambda _uid, _wid: None
    )

    await asyncio.wait_for(
        _session_create.create_and_activate_session(
            query, context, user, "/worker/project", node_id="worker-a"
        ),
        timeout=0.1,
    )
    context.user_data["_new_session_backend"] = "codex"
    context.user_data["_pending_session_name"] = "Changed later"
    await asyncio.wait_for(entered.wait(), timeout=0.1)
    assert fake_manager.create_session.call_count == 0

    release.set()
    await _session_create.wait_for_session_creation(user.id)
    assert fake_manager.create_session.call_count == 1
    assert received == [("worker-a", "/worker/project", "claude", "Original task")]


@pytest.mark.asyncio
async def test_failed_remote_creation_closes_its_startup_queue(monkeypatch) -> None:
    user_id = 43
    runtime = SimpleNamespace(
        create_session=AsyncMock(side_effect=RuntimeError("worker startup failed"))
    )
    fake_manager = SimpleNamespace(
        agent_backend="claude",
        get_active_session=lambda _uid: SimpleNamespace(id="old"),
    )
    query = MagicMock(spec=CallbackQuery)
    query.message = None
    query.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.id = user_id
    context = SimpleNamespace(user_data={}, bot=object())
    monkeypatch.setattr(_session_create, "session_manager", fake_manager)
    monkeypatch.setattr(_session_create, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(_session_create, "safe_edit", AsyncMock())
    begin_startup_queue(user_id)

    await _session_create.create_and_activate_session(
        query, context, user, "/worker/project", node_id="worker-a"
    )
    await _session_create.wait_for_session_creation(user_id)

    assert has_startup_queue(user_id) is False


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

    create_kwargs: dict[str, object] = {}

    async def create_window(*_args, **kwargs):
        create_kwargs.update(kwargs)
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

    await _session_create.create_and_activate_session(
        query, context, user, "/tmp/project"
    )
    try:
        await asyncio.wait_for(entered_create.wait(), timeout=1)
        assert old_state.in_menu_view is True
        assert old_state.msg_id == carrier_id
        release_create.set()
        await asyncio.wait_for(
            _session_create.wait_for_session_creation(user.id), timeout=1
        )
    finally:
        release_create.set()
        _cards.pop((user_id, old_session.id), None)

    assert transitions == ["handoff", "paint"]
    assert create_kwargs["wait_for_codex_ready"] is False


@pytest.mark.asyncio
async def test_new_session_is_created_on_selected_remote_node(monkeypatch):
    user_id = 42
    new_session = SimpleNamespace(id="remote-session", claude_session_id=None)
    runtime = SimpleNamespace(
        create_session=AsyncMock(
            return_value={
                "ok": True,
                "target_window_id": "@8",
                "target_workdir": "/worker/project",
                "target_agent_session_id": "agent-8",
            }
        )
    )
    fake_manager = SimpleNamespace(
        agent_backend="claude",
        get_active_session=lambda _uid: None,
        create_session=MagicMock(return_value=new_session),
        set_session_claude_id=MagicMock(),
        set_active_session=MagicMock(),
        save_state=MagicMock(),
    )
    query = MagicMock(spec=CallbackQuery)
    query.message = None
    query.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.id = user_id
    context = SimpleNamespace(
        user_data={"_new_session_backend": "claude", "_pending_session_name": "Task"},
        bot=object(),
    )

    monkeypatch.setattr(_session_create, "session_manager", fake_manager)
    monkeypatch.setattr(_session_create, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(
        "ccbot.startup_queue.bind_startup_queue", lambda _uid, _wid: None
    )

    await _session_create.create_and_activate_session(
        query, context, user, "/worker/project", node_id="worker-a"
    )
    await _session_create.wait_for_session_creation(user.id)

    runtime.create_session.assert_awaited_once_with(
        "worker-a", "/worker/project", "claude", "Task"
    )
    fake_manager.create_session.assert_called_once_with(
        name="Task",
        window_id="worker-a::@8",
        workdir="/worker/project",
        backend="claude",
        node_id="worker-a",
    )
    fake_manager.set_session_claude_id.assert_called_once_with(
        "remote-session", "agent-8"
    )
