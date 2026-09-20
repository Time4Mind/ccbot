from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from ccbot.bot import _common
from ccbot.bot.callbacks import more_menu, nodes
from ccbot.handlers import menu
from ccbot.handlers import nodes as nodes_view
from ccbot.handlers.callback_data import CB_FT_MORE, CB_MM_NODES, CB_NODE_USE, CB_SW_NEW
from ccbot.node_models import Node
from ccbot.session_models import Session


def _callbacks(markup) -> set[str | None]:
    return {button.callback_data for row in markup.inline_keyboard for button in row}


@pytest.mark.asyncio
async def test_empty_sessions_keeps_nodes_reachable_with_multiple_nodes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        _common.session_manager, "get_active_session", lambda _user_id: None
    )
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: True)
    edit = AsyncMock()
    monkeypatch.setattr(_common, "safe_edit", edit)
    query = SimpleNamespace(message=SimpleNamespace(message_id=77))

    await _common.open_sessions_in_place(query, object(), 42)

    keyboard = edit.await_args.kwargs["reply_markup"]
    assert CB_MM_NODES in _callbacks(keyboard)


@pytest.mark.asyncio
async def test_empty_sessions_omits_nodes_with_only_local_node(monkeypatch) -> None:
    monkeypatch.setattr(
        _common.session_manager, "get_active_session", lambda _user_id: None
    )
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: False)
    edit = AsyncMock()
    monkeypatch.setattr(_common, "safe_edit", edit)
    query = SimpleNamespace(message=SimpleNamespace(message_id=77))

    await _common.open_sessions_in_place(query, object(), 42)

    keyboard = edit.await_args.kwargs["reply_markup"]
    assert _callbacks(keyboard) == {CB_SW_NEW, CB_FT_MORE}


@pytest.mark.asyncio
async def test_empty_node_can_switch_to_another_node_directly(monkeypatch) -> None:
    registered = {
        "local": Node.local(),
        "worker-a": Node("worker-a", "Worker A", state="ready"),
    }
    manager = MagicMock()
    manager.get_node.side_effect = registered.get
    manager.get_active_session.return_value = None
    manager.get_selected_node_id.return_value = "worker-a"
    manager.list_nodes.return_value = list(registered.values())
    manager.list_user_sessions.return_value = []
    manager.has_multiple_nodes = True
    monkeypatch.setattr(_common, "session_manager", manager)
    monkeypatch.setattr(menu, "session_manager", manager)
    monkeypatch.setattr(nodes, "session_manager", manager)
    monkeypatch.setattr(nodes_view, "session_manager", manager)
    empty_edit = AsyncMock()
    nodes_edit = AsyncMock()
    monkeypatch.setattr(_common, "safe_edit", empty_edit)
    monkeypatch.setattr(more_menu, "safe_edit", nodes_edit)
    context = SimpleNamespace(bot=object(), user_data={})
    user = SimpleNamespace(id=42)

    assert await nodes.handle(_query(f"{CB_NODE_USE}worker-a"), context, user)
    empty_keyboard = empty_edit.await_args.kwargs["reply_markup"]
    assert CB_MM_NODES in _callbacks(empty_keyboard)

    assert await more_menu.handle(_query(CB_MM_NODES), context, user)
    assert await nodes.handle(_query(f"{CB_NODE_USE}local"), context, user)

    assert manager.set_selected_node.call_args_list == [
        call(42, "worker-a"),
        call(42, "local"),
    ]


def test_active_remote_session_still_exposes_nodes(monkeypatch) -> None:
    remote = Session(
        id="remote-session",
        name="Remote",
        window_id="@worker:1",
        state="active",
        node_id="worker-a",
    )
    monkeypatch.setattr(menu, "_has_multiple_nodes", lambda: True)
    monkeypatch.setattr(menu, "_has_active_session", lambda _user_id: True)
    monkeypatch.setattr(menu, "_has_pending_kb_action", lambda _user_id: False)
    monkeypatch.setattr(menu.session_manager, "get_active_session", lambda _uid: remote)

    keyboard = menu.build_footer_keyboard(42, screen="main")

    assert keyboard is not None
    assert CB_MM_NODES in _callbacks(keyboard)


def _query(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(message_id=77),
        answer=AsyncMock(),
    )
