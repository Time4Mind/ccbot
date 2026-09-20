from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.bot.callbacks import more_menu, nodes
from ccbot.bot.callbacks import callback_handler as dispatch_callback
from ccbot.handlers import notifications
from ccbot.handlers.callback_data import CB_MM_NODES
from ccbot.handlers.card_model import CardState
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


@pytest.fixture(autouse=True)
def _clear_cards() -> None:
    notifications._cards.clear()
    yield
    notifications._cards.clear()


def _session(session_id: str = "active") -> Session:
    return Session(
        id=session_id,
        name=session_id,
        window_id="@1",
        state="active",
    )


def _manager(active: Session | None) -> MagicMock:
    manager = MagicMock()
    manager.get_active_session.return_value = active
    manager.get_session.side_effect = (
        lambda session_id: active if active and session_id == active.id else None
    )
    return manager


def _query(data: str, message_id: int = 77) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(message_id=message_id),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_completed_card_direct_nodes_tap_survives_callback_epilogue(
    monkeypatch,
) -> None:
    from ccbot.bot import callbacks as dispatcher
    from ccbot.handlers import nodes as nodes_view

    user_id = 42
    sess = _session()
    state = CardState(msg_id=77, completion_marker_pending=True)
    notifications._cards[(user_id, sess.id)] = state
    manager = _manager(sess)
    visible: list[str] = []
    monkeypatch.setattr(dispatcher, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(dispatcher, "session_manager", manager)
    monkeypatch.setattr(more_menu, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes_view, "build_nodes_keyboard", lambda _uid: object())
    monkeypatch.setattr(
        more_menu,
        "safe_edit",
        AsyncMock(side_effect=lambda *_args, **_kwargs: visible.append("nodes")),
    )
    monkeypatch.setattr(
        dispatcher,
        "refresh_panel",
        AsyncMock(side_effect=lambda *_args, **_kwargs: visible.append("card")),
    )
    query = _query(CB_MM_NODES)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(bot=object(), user_data={})

    await dispatch_callback(update, context)

    assert visible == ["nodes"]
    assert state.completion_marker_pending is False
    assert state.in_menu_view is True


@pytest.mark.asyncio
async def test_live_event_buffers_while_direct_nodes_screen_is_visible(
    monkeypatch,
) -> None:
    from ccbot.handlers import nodes as nodes_view

    user_id = 42
    sess = _session()
    state = CardState(msg_id=77)
    notifications._cards[(user_id, sess.id)] = state
    manager = _manager(sess)
    monkeypatch.setattr(more_menu, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes_view, "build_nodes_keyboard", lambda _uid: object())
    monkeypatch.setattr(more_menu, "safe_edit", AsyncMock())
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock())
    monkeypatch.setattr(
        notifications.session_manager, "get_active_session", lambda _uid: sess
    )
    edit_card = AsyncMock()
    monkeypatch.setattr(notifications, "_edit_card", edit_card)
    context = SimpleNamespace(bot=object(), user_data={})

    assert await more_menu.handle(
        _query(CB_MM_NODES), context, SimpleNamespace(id=user_id)
    )
    await notifications.update_session_card(
        AsyncMock(),
        user_id,
        sess,
        NewMessage(
            session_id="provider-active",
            text="buffered event",
            is_complete=True,
            content_type="text",
            role="assistant",
            stop_reason="end_turn",
        ),
    )

    assert state.in_menu_view is True
    assert any(event.text == "buffered event" for event in state.events)
    edit_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_nodes_back_resumes_originating_session(monkeypatch) -> None:
    from ccbot.handlers import nodes as nodes_view

    user_id = 42
    sess = _session()
    state = CardState(msg_id=77)
    notifications._cards[(user_id, sess.id)] = state
    manager = _manager(sess)
    monkeypatch.setattr(more_menu, "session_manager", manager)
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes_view, "build_nodes_keyboard", lambda _uid: object())
    monkeypatch.setattr(more_menu, "safe_edit", AsyncMock())
    resume = AsyncMock()
    monkeypatch.setattr(nodes, "resume_card_view", resume, raising=False)
    context = SimpleNamespace(bot=object(), user_data={})
    user = SimpleNamespace(id=user_id)

    assert await more_menu.handle(_query(CB_MM_NODES), context, user)
    assert await nodes.handle(_query("nd:back"), context, user)

    resume.assert_awaited_once_with(context.bot, user_id, sess)


@pytest.mark.asyncio
async def test_menu_nodes_back_returns_to_menu(monkeypatch) -> None:
    from ccbot.handlers import nodes as nodes_view

    user_id = 42
    sess = _session()
    state = CardState(msg_id=77, in_menu_view=True)
    notifications._cards[(user_id, sess.id)] = state
    manager = _manager(sess)
    monkeypatch.setattr(more_menu, "session_manager", manager)
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes_view, "build_nodes_keyboard", lambda _uid: object())
    monkeypatch.setattr(more_menu, "safe_edit", AsyncMock())
    open_menu = AsyncMock()
    monkeypatch.setattr(nodes, "open_more_in_place", open_menu, raising=False)
    context = SimpleNamespace(bot=object(), user_data={})
    user = SimpleNamespace(id=user_id)

    assert await more_menu.handle(_query(CB_MM_NODES), context, user)
    assert await nodes.handle(_query("nd:back"), context, user)

    open_menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_direct_nodes_origin_falls_back_to_menu(monkeypatch) -> None:
    from ccbot.handlers import nodes as nodes_view

    user_id = 42
    origin = _session("origin")
    current = _session("current")
    state = CardState(msg_id=77)
    notifications._cards[(user_id, origin.id)] = state
    manager = _manager(origin)
    monkeypatch.setattr(more_menu, "session_manager", manager)
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "render_nodes_text", lambda _uid: "nodes")
    monkeypatch.setattr(nodes_view, "build_nodes_keyboard", lambda _uid: object())
    monkeypatch.setattr(more_menu, "safe_edit", AsyncMock())
    resume = AsyncMock()
    open_menu = AsyncMock()
    monkeypatch.setattr(nodes, "resume_card_view", resume, raising=False)
    monkeypatch.setattr(nodes, "open_more_in_place", open_menu, raising=False)
    context = SimpleNamespace(bot=object(), user_data={})
    user = SimpleNamespace(id=user_id)

    assert await more_menu.handle(_query(CB_MM_NODES), context, user)
    manager.get_active_session.return_value = current
    manager.get_session.return_value = origin
    assert await nodes.handle(_query("nd:back"), context, user)

    resume.assert_not_awaited()
    open_menu.assert_awaited_once()
